"""Eval metrics scoring/aggregation (SPEC.md §6 Phase 4 task 2), unit-level
— `QuestionResult`s are constructed directly here rather than run through
the full pipeline (that integration is covered by test_eval_run.py)."""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.db import get_repo_connection
from app.eval.golden import GoldenQuestion
from app.eval.metrics import (
    QuestionResult,
    QuestionScore,
    _percentile,
    aggregate,
    aggregate_by_route,
    score_question,
)
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider

from .conftest import MINI_REPO, make_settings

_COST_KWARGS = {"cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015}


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    return get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)


def _question(**overrides) -> GoldenQuestion:
    defaults = {
        "id": "q1", "question": "Where is UserService defined?", "route": "locate",
        "expected_files": ["src/users/service.py"], "expected_substrings": ["UserService"], "notes": None,
    }
    defaults.update(overrides)
    return GoldenQuestion(**defaults)


def _result(**overrides) -> QuestionResult:
    defaults = {
        "question": _question(), "route_taken": "locate", "answer_text": "answer",
        "context_paths": set(), "latency_ms": 100, "input_tokens": None, "output_tokens": None, "error": None,
    }
    defaults.update(overrides)
    return QuestionResult(**defaults)


# --- recall ---


def test_score_question_recall_hit_true_when_expected_file_in_context(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    result = _result(context_paths={"src/users/service.py", "other.py"})
    score = score_question(conn, None, result, **_COST_KWARGS)
    assert score.recall_hit is True


def test_score_question_recall_hit_false_when_expected_file_missing(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    result = _result(context_paths={"unrelated.py"})
    score = score_question(conn, None, result, **_COST_KWARGS)
    assert score.recall_hit is False


def test_score_question_recall_none_when_no_expected_files(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    result = _result(question=_question(expected_files=[]), context_paths=set())
    score = score_question(conn, None, result, **_COST_KWARGS)
    assert score.recall_hit is None


# --- citation validity ---


def test_score_question_citation_validity_all_valid(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    result = _result(answer_text="UserService is at [src/users/service.py:21-44].")
    score = score_question(conn, None, result, **_COST_KWARGS)
    assert score.citation_validity == 1.0


def test_score_question_citation_validity_mixed(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    result = _result(
        answer_text="See [src/users/service.py:21-44] and also [src/does/not/exist.py:1-5]."
    )
    score = score_question(conn, None, result, **_COST_KWARGS)
    assert score.citation_validity == 0.5


def test_score_question_citation_validity_none_when_no_citations(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    result = _result(answer_text="No citations here at all.")
    score = score_question(conn, None, result, **_COST_KWARGS)
    assert score.citation_validity is None


# --- correctness (LLM judge) ---


def test_score_question_correctness_parses_judge_score(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    judge = FakeLLMProvider(responses=["2"])
    score = score_question(conn, judge, _result(), **_COST_KWARGS)
    assert score.correctness == 2


def test_score_question_correctness_none_when_no_judge_provided(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    score = score_question(conn, None, _result(), **_COST_KWARGS)
    assert score.correctness is None


def test_score_question_correctness_none_when_judge_response_unparseable(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    judge = FakeLLMProvider(responses=["I refuse to answer with a number"])
    score = score_question(conn, judge, _result(), **_COST_KWARGS)
    assert score.correctness is None


def test_score_question_correctness_skipped_when_question_errored(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    judge = FakeLLMProvider(responses=["2"])
    score = score_question(conn, judge, _result(error="boom"), **_COST_KWARGS)
    assert score.correctness is None
    assert score.error == "boom"


# --- cost ---


def test_score_question_computes_cost_from_tokens(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    result = _result(input_tokens=1000, output_tokens=1000)
    score = score_question(conn, None, result, cost_per_1k_input=0.003, cost_per_1k_output=0.015)
    assert score.cost_usd == 0.003 + 0.015


def test_score_question_cost_none_without_token_counts(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    score = score_question(conn, None, _result(), **_COST_KWARGS)
    assert score.cost_usd is None


# --- aggregation ---


def _score(**overrides) -> QuestionScore:
    defaults = {
        "question_id": "q", "route": "locate", "recall_hit": True, "citation_validity": 1.0,
        "correctness": 2, "latency_ms": 100, "input_tokens": 10, "output_tokens": 10,
        "cost_usd": 0.001, "error": None,
    }
    defaults.update(overrides)
    return QuestionScore(**defaults)


def test_aggregate_recall_at_k_averages_only_non_none():
    scores = [_score(recall_hit=True), _score(recall_hit=False), _score(recall_hit=None)]
    agg = aggregate(scores)
    assert agg.recall_at_k == 0.5  # 1 hit out of 2 scored (the None is excluded, not counted as a miss)


def test_aggregate_counts_errors():
    scores = [_score(error=None), _score(error="boom")]
    agg = aggregate(scores)
    assert agg.n_errors == 1
    assert agg.n_questions == 2


def test_aggregate_sums_tokens_and_cost():
    scores = [_score(input_tokens=100, output_tokens=50, cost_usd=0.01), _score(input_tokens=200, output_tokens=100, cost_usd=0.02)]
    agg = aggregate(scores)
    assert agg.total_input_tokens == 300
    assert agg.total_output_tokens == 150
    assert agg.total_cost_usd == 0.03


def test_aggregate_by_route_groups_correctly():
    scores = [_score(route="locate"), _score(route="locate"), _score(route="explain")]
    by_route = aggregate_by_route(scores)
    assert by_route["locate"].n_questions == 2
    assert by_route["explain"].n_questions == 1


def test_percentile_empty_list():
    assert _percentile([], 0.5) == 0.0


def test_percentile_single_value():
    assert _percentile([42.0], 0.95) == 42.0


def test_percentile_p50_and_p95():
    values = [float(i) for i in range(1, 101)]  # 1..100
    assert _percentile(values, 0.5) == 50.5
    assert _percentile(values, 0.95) == 95.05
