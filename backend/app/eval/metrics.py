"""Eval metrics (SPEC.md §6 Phase 4 task 2): score one golden question's
real-pipeline output, then aggregate scores overall and per-route.

`QuestionResult` is the raw material `run_eval.py` collects by actually
running a question through `generation/answer.py::generate_answer_events`
(the exact same code path the UI uses — no separate "eval mode" query
path to keep in sync). `score_question`/`aggregate` turn that into numbers;
neither function here calls the query pipeline itself.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from app.eval.golden import GoldenQuestion
from app.eval.judge_prompts import JUDGE_SYSTEM_PROMPT, build_judge_user_message
from app.generation.citations import verify_citations
from app.providers.llm import LLMProvider


@dataclass(frozen=True)
class QuestionResult:
    question: GoldenQuestion
    route_taken: str
    answer_text: str
    # Every file path the pipeline's process touched or cited for this
    # question — sources, tool-call read_file/find_files targets, and
    # final citations — the "final context" recall@k checks against.
    context_paths: set[str]
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class QuestionScore:
    question_id: str
    route: str
    recall_hit: bool | None  # None: question had no expected_files (N/A, excluded from the aggregate)
    citation_validity: float | None  # None: answer had zero citations (N/A)
    correctness: int | None  # 0/1/2; None if judging was skipped/failed
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    error: str | None = None


_SCORE_RE = re.compile(r"[012]")


def score_question(
    conn: sqlite3.Connection,
    judge_llm: LLMProvider | None,
    result: QuestionResult,
    *,
    cost_per_1k_input: float,
    cost_per_1k_output: float,
) -> QuestionScore:
    q = result.question
    recall_hit = any(f in result.context_paths for f in q.expected_files) if q.expected_files else None

    verification = verify_citations(conn, result.answer_text)
    total_citations = len(verification.citations) + verification.invalid_count
    citation_validity = (len(verification.citations) / total_citations) if total_citations else None

    correctness = None
    if result.error is None and judge_llm is not None:
        correctness = _judge_correctness(judge_llm, q, result.answer_text)

    cost_usd = None
    if result.input_tokens is not None and result.output_tokens is not None:
        cost_usd = (
            (result.input_tokens / 1000) * cost_per_1k_input
            + (result.output_tokens / 1000) * cost_per_1k_output
        )

    return QuestionScore(
        question_id=q.id,
        route=result.route_taken,
        recall_hit=recall_hit,
        citation_validity=citation_validity,
        correctness=correctness,
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=cost_usd,
        error=result.error,
    )


def _judge_correctness(judge_llm: LLMProvider, question: GoldenQuestion, answer_text: str) -> int | None:
    try:
        resp = judge_llm.complete(
            system=JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_judge_user_message(question, answer_text)}],
            max_tokens=10,
        )
    except Exception:  # noqa: BLE001 - a judge failure shouldn't crash the whole eval run
        return None
    match = _SCORE_RE.search(resp.text)
    return int(match.group()) if match else None


@dataclass(frozen=True)
class AggregateMetrics:
    n_questions: int
    n_errors: int
    recall_at_k: float | None  # fraction of questions-with-expected_files that hit
    citation_validity: float | None  # mean over questions that had >=1 citation
    mean_correctness: float | None  # mean of 0/1/2 over judged questions
    latency_p50_ms: float
    latency_p95_ms: float
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile — no numpy dependency for one function."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = pct * (len(ordered) - 1)
    lo, hi = int(rank), min(int(rank) + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(scores: list[QuestionScore]) -> AggregateMetrics:
    recall_scores = [s.recall_hit for s in scores if s.recall_hit is not None]
    citation_scores = [s.citation_validity for s in scores if s.citation_validity is not None]
    correctness_scores = [s.correctness for s in scores if s.correctness is not None]
    latencies = [float(s.latency_ms) for s in scores]

    return AggregateMetrics(
        n_questions=len(scores),
        n_errors=sum(1 for s in scores if s.error is not None),
        recall_at_k=(sum(recall_scores) / len(recall_scores)) if recall_scores else None,
        citation_validity=_mean(citation_scores),
        mean_correctness=_mean([float(c) for c in correctness_scores]),
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
        total_input_tokens=sum(s.input_tokens or 0 for s in scores),
        total_output_tokens=sum(s.output_tokens or 0 for s in scores),
        total_cost_usd=sum(s.cost_usd or 0.0 for s in scores),
    )


def aggregate_by_route(scores: list[QuestionScore]) -> dict[str, AggregateMetrics]:
    by_route: dict[str, list[QuestionScore]] = {}
    for s in scores:
        by_route.setdefault(s.route, []).append(s)
    return {route: aggregate(rows) for route, rows in by_route.items()}
