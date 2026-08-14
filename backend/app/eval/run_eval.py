"""Eval CLI (SPEC.md §6 Phase 4 tasks 2-3): `python -m app.eval.run_eval --repo mini_repo`.

Indexes (or reuses an already-indexed) target repo, runs its golden set
through the exact same query pipeline the UI uses
(`generation/answer.py::generate_answer_events` — no separate "eval mode"
code path to drift out of sync), scores each answer, and writes a JSON
report plus a markdown diff against the previous report for the same repo.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from app import jobs
from app.config import Settings, get_settings
from app.db import get_registry_connection, get_repo_connection
from app.eval.golden import GoldenQuestion, golden_set_path, load_golden_set
from app.eval.metrics import (
    QuestionResult,
    QuestionScore,
    aggregate,
    aggregate_by_route,
    score_question,
)
from app.generation.answer import generate_answer_events
from app.generation.citations import RepoContext
from app.providers.embeddings import EmbeddingProvider, get_embedding_provider
from app.providers.llm import LLMProvider, get_llm_provider, get_summarization_llm_provider
from app.util import now_iso

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MINI_REPO_PATH = _PROJECT_ROOT / "backend" / "tests" / "fixtures" / "mini_repo"

# SPEC.md §8's recommended test repos. mini_repo is local (no network, no
# API rate limits — the one that can run in CI); the rest are real GitHub
# repos for evaluating against genuine, messy code.
REPO_SOURCES: dict[str, str] = {
    "mini_repo": str(_MINI_REPO_PATH),
    "flask": "https://github.com/pallets/flask",
}


def run_eval(
    settings: Settings,
    repo_name: str,
    *,
    reindex: bool = False,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    judge_llm_provider: LLMProvider | None = None,
) -> dict:
    """CLI-facing (`python -m app.eval.run_eval --repo <name>`): resolves
    `repo_name` to one of SPEC.md §8's recommended test repos, indexes it
    if there's no ready index yet (or `reindex` is set), and evaluates
    against `eval/golden/{repo_name}.yaml` — `repo_name` doubles as the
    golden-set suite name here. See `run_eval_against_repo` for the API-
    facing entry point (SPEC.md §5's `POST /api/eval/run {repo_id, suite}`),
    which skips indexing entirely and evaluates an already-indexed repo.
    """
    if repo_name not in REPO_SOURCES:
        raise ValueError(f"unknown eval repo {repo_name!r} (known: {sorted(REPO_SOURCES)})")

    embedding_provider = embedding_provider or get_embedding_provider(settings)
    repo_id, index_seconds = _ensure_indexed(settings, repo_name, reindex=reindex)
    return run_eval_against_repo(
        settings, repo_id, repo_name,
        embedding_provider=embedding_provider, llm_provider=llm_provider,
        judge_llm_provider=judge_llm_provider, index_seconds=index_seconds,
    )


def run_eval_against_repo(
    settings: Settings,
    repo_id: str,
    suite: str,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    judge_llm_provider: LLMProvider | None = None,
    index_seconds: float | None = None,
) -> dict:
    """API-facing (`POST /api/eval/run`): `repo_id` must already be a
    `ready` repo in the registry — this never indexes anything itself.
    `suite` picks which `eval/golden/{suite}.yaml` to score it against
    (independent of whatever `repo_id`'s own source was — you could in
    principle score any indexed repo against any golden set, though in
    practice `suite` matching the repo's own content is what makes the
    `expected_files`/`expected_substrings` meaningful). `index_seconds`
    lets a caller who just froshly indexed `repo_id` (like `run_eval`
    above) pass along how long that took, for the `index_time_per_mb`
    metric — omit it for an already-indexed repo, where there's nothing
    to time.
    """
    embedding_provider = embedding_provider or get_embedding_provider(settings)
    llm_provider = llm_provider or get_llm_provider(settings)
    if judge_llm_provider is None:
        try:
            judge_llm_provider = get_summarization_llm_provider(settings)
        except RuntimeError:
            judge_llm_provider = None  # correctness scoring skipped, not fatal to the rest of the report

    questions = load_golden_set(golden_set_path(settings.golden_dir, suite))

    registry_conn = get_registry_connection(settings)
    repo_row = registry_conn.execute(
        "SELECT source_type, display_name, commit_sha, local_path, status FROM repos WHERE id = ?",
        (repo_id,),
    ).fetchone()
    if repo_row is None:
        raise ValueError(f"repo {repo_id!r} not found")
    if repo_row["status"] != "ready":
        raise RuntimeError(f"repo {repo_id!r} is not ready (status={repo_row['status']})")

    repo_context = RepoContext(
        source_type=repo_row["source_type"],
        owner_repo=repo_row["display_name"] if repo_row["source_type"] == "github" else None,
        commit_sha=repo_row["commit_sha"],
    )
    repo_root = Path(repo_row["local_path"])
    repo_conn = get_repo_connection(repo_id, embedding_provider.dim, settings)

    repo_size_bytes = repo_conn.execute("SELECT SUM(size_bytes) AS n FROM files").fetchone()["n"] or 0

    scores: list[QuestionScore] = []
    for q in questions:
        result = _run_question(repo_conn, settings, embedding_provider, llm_provider, repo_context, repo_root, q)
        scores.append(
            score_question(
                repo_conn, judge_llm_provider, result,
                cost_per_1k_input=settings.cost_per_1k_input_tokens_usd,
                cost_per_1k_output=settings.cost_per_1k_output_tokens_usd,
            )
        )

    overall = aggregate(scores)
    by_route = aggregate_by_route(scores)
    index_time_per_mb = (
        (index_seconds / (repo_size_bytes / 1_000_000)) if (index_seconds and repo_size_bytes) else None
    )

    report = {
        "repo": suite,
        "repo_id": repo_id,
        "timestamp": now_iso(),
        "n_questions": overall.n_questions,
        "overall": asdict(overall),
        "by_route": {route: asdict(m) for route, m in by_route.items()},
        "index_time_seconds": index_seconds,
        "index_time_per_mb": index_time_per_mb,
        "questions": [asdict(s) for s in scores],
    }
    _write_report(settings, suite, report)
    return report


def _ensure_indexed(settings: Settings, repo_name: str, *, reindex: bool) -> tuple[str, float | None]:
    """Reuses an already-`ready` repo for this source unless `reindex` is
    set, so re-running the eval repeatedly doesn't reclone/reindex every
    time. Returns (repo_id, index_seconds) — `index_seconds` is None when
    an existing index was reused (no fresh indexing happened to time)."""
    source = REPO_SOURCES[repo_name]
    registry_conn = get_registry_connection(settings)

    if not reindex:
        existing = registry_conn.execute(
            "SELECT id FROM repos WHERE source = ? AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
            (source,),
        ).fetchone()
        if existing is not None:
            return existing["id"], None

    repo_id, job_id = jobs.create_repo_and_job(source, settings)
    start = time.monotonic()
    jobs.run_index_job(job_id, repo_id, source, settings)
    elapsed = time.monotonic() - start

    row = registry_conn.execute("SELECT status, error FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row["status"] != "ready":
        raise RuntimeError(f"failed to index {repo_name!r}: {row['error']}")
    return repo_id, elapsed


def _run_question(
    conn: sqlite3.Connection,
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    repo_context: RepoContext,
    repo_root: Path,
    question: GoldenQuestion,
) -> QuestionResult:
    start = time.monotonic()
    answer_parts: list[str] = []
    context_paths: set[str] = set()
    route_taken = "unknown"
    query_id: int | None = None
    error: str | None = None

    try:
        for event in generate_answer_events(
            conn, settings, embedding_provider, llm_provider, repo_context, repo_root,
            question=question.question,
        ):
            data = json.loads(event["data"])
            match event["event"]:
                case "sources":
                    context_paths.update(c["path"] for c in data["chunks"])
                case "tool":
                    path = (data.get("input") or {}).get("path")
                    if path:
                        context_paths.add(path)
                case "token":
                    answer_parts.append(data["text"])
                case "citations":
                    context_paths.update(c["path"] for c in data["citations"])
                case "done":
                    route_taken = data["route"]
                    query_id = data["query_id"]
                case "error":
                    error = data["message"]
    except Exception as exc:  # noqa: BLE001 - one bad question shouldn't abort the whole eval run
        error = str(exc)

    latency_ms = int((time.monotonic() - start) * 1000)
    input_tokens = output_tokens = None
    if query_id is not None:
        usage_row = conn.execute(
            "SELECT input_tokens, output_tokens FROM query_log WHERE id = ?", (query_id,)
        ).fetchone()
        if usage_row is not None:
            input_tokens, output_tokens = usage_row["input_tokens"], usage_row["output_tokens"]

    return QuestionResult(
        question=question,
        route_taken=route_taken,
        answer_text="".join(answer_parts),
        context_paths=context_paths,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=error,
    )


def _write_report(settings: Settings, repo_name: str, report: dict) -> Path:
    settings.eval_report_dir.mkdir(parents=True, exist_ok=True)
    safe_timestamp = report["timestamp"].replace(":", "").replace("-", "")
    path = settings.eval_report_dir / f"{repo_name}-{safe_timestamp}.json"
    path.write_text(json.dumps(report, indent=2))
    _write_markdown_diff(settings, repo_name, report)
    return path


_DIFF_METRICS = ["recall_at_k", "citation_validity", "mean_correctness", "latency_p50_ms", "latency_p95_ms", "total_cost_usd"]


def _write_markdown_diff(settings: Settings, repo_name: str, report: dict) -> None:
    previous = _find_previous_report(settings, repo_name, current_timestamp=report["timestamp"])
    lines = [f"# Eval report: {repo_name}", "", f"Generated: {report['timestamp']}", ""]

    if previous is None:
        lines.append("| Metric | Current |")
        lines.append("|---|---|")
        for key in _DIFF_METRICS:
            lines.append(f"| {key} | {report['overall'].get(key)} |")
    else:
        lines.append("| Metric | Current | Previous | Δ |")
        lines.append("|---|---|---|---|")
        for key in _DIFF_METRICS:
            cur, prev = report["overall"].get(key), previous["overall"].get(key)
            delta = f"{cur - prev:+.3f}" if isinstance(cur, (int, float)) and isinstance(prev, (int, float)) else "n/a"
            lines.append(f"| {key} | {cur} | {prev} | {delta} |")

    (settings.eval_report_dir / f"{repo_name}-latest.md").write_text("\n".join(lines) + "\n")


def _find_previous_report(settings: Settings, repo_name: str, *, current_timestamp: str) -> dict | None:
    if not settings.eval_report_dir.is_dir():
        return None
    candidates = sorted(
        p for p in settings.eval_report_dir.glob(f"{repo_name}-*.json")
        if p.stem != f"{repo_name}-{current_timestamp.replace(':', '').replace('-', '')}"
    )
    if not candidates:
        return None
    return json.loads(candidates[-1].read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the golden-set eval against an indexed repo.")
    parser.add_argument("--repo", required=True, choices=sorted(REPO_SOURCES), help="which golden set to run")
    parser.add_argument("--reindex", action="store_true", help="force a fresh index even if one already exists")
    args = parser.parse_args()

    settings = get_settings()
    report = run_eval(settings, args.repo, reindex=args.reindex)

    print(json.dumps(report["overall"], indent=2))
    print("\nBy route:")
    for route, metrics in report["by_route"].items():
        print(f"  {route}: {json.dumps(metrics)}")


if __name__ == "__main__":
    main()
