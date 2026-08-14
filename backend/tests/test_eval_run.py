"""End-to-end eval run (SPEC.md §6 Phase 4 tasks 2-3), zero network: proves
`run_eval()` produces a full report with a per-route breakdown against the
real mini_repo golden set, using Fake providers throughout — the exact same
`generate_answer_events` pipeline the UI uses, not a separate eval-only path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.eval.run_eval import run_eval
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider, ToolUseResponse

from .conftest import make_settings


# mini_repo's golden set has 5 "explain" questions, and 5 "overview"
# questions that all fall back to the agent path too (no ANTHROPIC_API_KEY
# in test settings means jobs.py skips summarization entirely, so no
# cached repo-level summary ever exists here) — 10 agent-path questions,
# each needing one scripted immediate-answer response. A generous 30-entry
# script covers that plus any `locate` question that unexpectedly falls
# back (defensive, not expected to be needed).
def _scripted_llm(n: int = 30) -> FakeLLMProvider:
    return FakeLLMProvider(tool_responses=[ToolUseResponse(text=f"Answer {i}.", tool_calls=[]) for i in range(n)])


def _settings(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    settings.eval_report_dir = tmp_path / "eval_report"
    return settings


def test_run_eval_produces_full_report_for_mini_repo(tmp_path: Path):
    settings = _settings(tmp_path)
    report = run_eval(
        settings, "mini_repo",
        embedding_provider=FakeEmbeddingProvider(),
        llm_provider=_scripted_llm(),
        judge_llm_provider=FakeLLMProvider(),
    )

    assert report["repo"] == "mini_repo"
    assert report["n_questions"] == 25
    assert set(report["by_route"]) <= {"dependencies", "endpoints", "locate", "explain", "overview"}
    assert report["overall"]["n_errors"] == 0
    assert report["overall"]["recall_at_k"] is not None
    assert report["overall"]["latency_p50_ms"] >= 0
    assert len(report["questions"]) == 25


def test_run_eval_structured_routes_have_high_recall(tmp_path: Path):
    """dependencies/endpoints/locate never depend on the agent or a real
    model to get retrieval right — their recall@k should be perfect."""
    settings = _settings(tmp_path)
    report = run_eval(
        settings, "mini_repo",
        embedding_provider=FakeEmbeddingProvider(), llm_provider=_scripted_llm(), judge_llm_provider=FakeLLMProvider(),
    )
    for route in ("dependencies", "endpoints", "locate"):
        assert report["by_route"][route]["recall_at_k"] == 1.0, report["by_route"][route]


def test_run_eval_writes_json_and_markdown_reports(tmp_path: Path):
    settings = _settings(tmp_path)
    run_eval(
        settings, "mini_repo",
        embedding_provider=FakeEmbeddingProvider(), llm_provider=_scripted_llm(), judge_llm_provider=FakeLLMProvider(),
    )
    json_reports = list(settings.eval_report_dir.glob("mini_repo-*.json"))
    assert len(json_reports) == 1
    assert (settings.eval_report_dir / "mini_repo-latest.md").is_file()


def test_run_eval_reuses_existing_index_on_second_call(tmp_path: Path):
    settings = _settings(tmp_path)
    first = run_eval(
        settings, "mini_repo",
        embedding_provider=FakeEmbeddingProvider(), llm_provider=_scripted_llm(), judge_llm_provider=FakeLLMProvider(),
    )
    assert first["index_time_seconds"] is not None

    second = run_eval(
        settings, "mini_repo",
        embedding_provider=FakeEmbeddingProvider(), llm_provider=_scripted_llm(), judge_llm_provider=FakeLLMProvider(),
    )
    assert second["index_time_seconds"] is None  # reused the existing ready index


def test_run_eval_writes_a_markdown_diff_against_the_previous_report(tmp_path: Path):
    settings = _settings(tmp_path)
    run_eval(
        settings, "mini_repo",
        embedding_provider=FakeEmbeddingProvider(), llm_provider=_scripted_llm(), judge_llm_provider=FakeLLMProvider(),
    )
    run_eval(
        settings, "mini_repo",
        embedding_provider=FakeEmbeddingProvider(), llm_provider=_scripted_llm(), judge_llm_provider=FakeLLMProvider(),
    )
    md = (settings.eval_report_dir / "mini_repo-latest.md").read_text()
    assert "Previous" in md

    # Only one JSON report gets a corresponding markdown snapshot at a
    # time (it's always "latest"), but both runs' JSON reports exist.
    assert len(list(settings.eval_report_dir.glob("mini_repo-*.json"))) == 2


def test_run_eval_unknown_repo_raises(tmp_path: Path):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match="unknown eval repo"):
        run_eval(settings, "does-not-exist", embedding_provider=FakeEmbeddingProvider(), llm_provider=FakeLLMProvider())
