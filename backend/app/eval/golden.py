"""Golden-set loading (SPEC.md §6 Phase 4 task 1): `eval/golden/{repo}.yaml`
— a plain list of questions with expected answers, one file per test repo.

Kept deliberately dumb: this module only parses and validates shape, it
never touches the query pipeline (that's `run_eval.py`) or scores anything
(that's `metrics.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

VALID_ROUTES = {"dependencies", "endpoints", "locate", "explain", "overview"}


class GoldenSetError(Exception):
    pass


@dataclass(frozen=True)
class GoldenQuestion:
    id: str
    question: str
    route: str
    expected_files: list[str]
    expected_substrings: list[str]
    notes: str | None = None


def load_golden_set(path: Path) -> list[GoldenQuestion]:
    """Every entry must have at least `id`/`question`/`route`; empty
    `expected_files`/`expected_substrings` are allowed (some questions —
    e.g. "what does this repo do?" — don't pin to one file), but a totally
    empty golden set or a bad `route` value is almost certainly a typo, so
    those raise rather than silently produce a report with nothing in it.
    """
    if not path.is_file():
        raise GoldenSetError(f"golden set not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise GoldenSetError(f"{path} must contain a non-empty YAML list")

    questions: list[GoldenQuestion] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise GoldenSetError(f"{path}: entry {i} is not a mapping")
        missing = [k for k in ("id", "question", "route") if k not in entry]
        if missing:
            raise GoldenSetError(f"{path}: entry {i} missing required field(s): {missing}")
        qid, route = entry["id"], entry["route"]
        if route not in VALID_ROUTES:
            raise GoldenSetError(f"{path}: entry {qid!r} has invalid route {route!r} (expected one of {sorted(VALID_ROUTES)})")
        if qid in seen_ids:
            raise GoldenSetError(f"{path}: duplicate question id {qid!r}")
        seen_ids.add(qid)
        questions.append(
            GoldenQuestion(
                id=qid,
                question=entry["question"],
                route=route,
                expected_files=list(entry.get("expected_files") or []),
                expected_substrings=list(entry.get("expected_substrings") or []),
                notes=entry.get("notes"),
            )
        )
    return questions


def golden_set_path(golden_dir: Path, repo: str) -> Path:
    return golden_dir / f"{repo}.yaml"
