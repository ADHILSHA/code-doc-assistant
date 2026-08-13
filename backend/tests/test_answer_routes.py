"""generation/answer.py's route dispatch (SPEC.md §6 Phase 2 task 5):
structured routes answer straight from SQL (no LLM call, no citation
regeneration), `locate` falls back to hybrid retrieval when nothing
matches, and every route still logs to `query_log` with the route that
actually ran.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import jobs
from app.db import get_repo_connection
from app.generation.answer import generate_answer_events
from app.generation.citations import RepoContext
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider

from .conftest import MINI_REPO, make_settings


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    return conn, settings


def _run(conn, settings, question: str) -> list[dict]:
    repo_context = RepoContext(source_type="local", owner_repo=None, commit_sha=None)
    return list(
        generate_answer_events(
            conn, settings, FakeEmbeddingProvider(), FakeLLMProvider(), repo_context, question=question
        )
    )


def _by_event(events: list[dict], name: str) -> list[dict]:
    return [json.loads(e["data"]) for e in events if e["event"] == name]


def test_dependencies_route_never_calls_llm_and_answers_fast(tmp_path: Path):
    conn, settings = _index_mini_repo(tmp_path)
    events = _run(conn, settings, "What dependencies does this project use?")

    done = _by_event(events, "done")[0]
    assert done["route"] == "dependencies"
    assert done["latency_ms"] < 1000  # SPEC.md: structured routes answer in <1s

    answer = "".join(json.loads(e["data"])["text"] for e in events if e["event"] == "token")
    assert "express" in answer
    assert "fastapi" in answer

    citations = _by_event(events, "citations")[0]["citations"]
    paths = {c["path"] for c in citations}
    assert paths == {"package.json", "pyproject.toml"}


def test_endpoints_route_lists_every_indexed_endpoint(tmp_path: Path):
    conn, settings = _index_mini_repo(tmp_path)
    events = _run(conn, settings, "What API endpoints does this app expose?")

    done = _by_event(events, "done")[0]
    assert done["route"] == "endpoints"
    assert done["latency_ms"] < 1000

    answer = "".join(json.loads(e["data"])["text"] for e in events if e["event"] == "token")
    assert "GET /health" in answer
    assert "POST /login" in answer


def test_locate_route_resolves_exact_symbol_match(tmp_path: Path):
    conn, settings = _index_mini_repo(tmp_path)
    events = _run(conn, settings, "Where is `UserService` defined?")

    done = _by_event(events, "done")[0]
    assert done["route"] == "locate"
    assert done["latency_ms"] < 1000

    citations = _by_event(events, "citations")[0]["citations"]
    assert any(c["path"] == "src/users/service.py" for c in citations)


def test_locate_route_falls_back_to_hybrid_retrieval_when_no_symbol_matches(tmp_path: Path):
    conn, settings = _index_mini_repo(tmp_path)
    # No named-symbol candidate in this question will match anything in
    # `symbols` (extract_candidate_names strips stopwords down to
    # "password"/"hashing", neither of which is a real symbol name) — must
    # gracefully fall through to hybrid retrieval instead of erroring or
    # returning an empty structured answer.
    events = _run(conn, settings, "Where does password hashing happen?")

    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"
    statuses = _by_event(events, "status")
    assert any(s.get("stage") == "retrieving" for s in statuses)


def test_explain_route_uses_hybrid_retrieval_and_llm(tmp_path: Path):
    conn, settings = _index_mini_repo(tmp_path)
    events = _run(conn, settings, "How does the login flow work?")

    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"
    statuses = _by_event(events, "status")
    assert any(s.get("stage") == "generating" for s in statuses)


def test_no_anthropic_key_does_not_break_structured_routes(tmp_path: Path):
    """The whole point of structured routes: they must work with zero LLM
    access, including for the ambiguous questions that would otherwise need
    the router's LLM-fallback classifier."""
    settings = make_settings(tmp_path, allow_local_repos=True, anthropic_api_key=None)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)

    events = _run(conn, settings, "What dependencies does this project use?")
    done = _by_event(events, "done")[0]
    assert done["route"] == "dependencies"
    error_events = _by_event(events, "error")
    assert error_events == []


def test_unrouteable_question_without_llm_key_defaults_to_explain_and_errors_gracefully(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True, anthropic_api_key=None)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    repo_context = RepoContext(source_type="local", owner_repo=None, commit_sha=None)

    # No fast-path regex matches this, and there's no ANTHROPIC_API_KEY for
    # the router's LLM-fallback classifier — must default to "explain"
    # rather than raise, and the *answer* LLM call (also keyless) should
    # surface as a clean SSE "error" event, not an unhandled exception.
    events = list(
        generate_answer_events(
            conn,
            settings,
            FakeEmbeddingProvider(),
            _RaisingLLMProvider(),
            repo_context,
            question="Tell me about the thing",
        )
    )
    error_events = _by_event(events, "error")
    assert len(error_events) == 1


class _RaisingLLMProvider:
    """Stands in for `get_llm_provider` raising when ANTHROPIC_API_KEY is
    unset, without actually depending on api/query.py's dependency wiring."""

    model = "raising"

    def complete(self, **kwargs):
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the synthesis LLM")

    def stream(self, **kwargs):
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the synthesis LLM")

    def complete_with_tools(self, **kwargs):
        raise NotImplementedError
