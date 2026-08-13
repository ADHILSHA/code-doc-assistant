"""generation/answer.py's route dispatch (SPEC.md §6 Phase 2 task 5 /
Phase 3): structured routes answer straight from SQL (no LLM call, no
citation regeneration); `locate`/`overview` fall back to the agent path
when nothing structured is available; every route still logs to
`query_log` with the route that actually ran.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import jobs
from app.db import get_registry_connection, get_repo_connection
from app.generation.answer import generate_answer_events
from app.generation.citations import RepoContext
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider, ToolUseResponse

from .conftest import MINI_REPO, make_settings


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    registry_conn = get_registry_connection(settings)
    local_path = registry_conn.execute(
        "SELECT local_path FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["local_path"]
    return conn, settings, Path(local_path)


def _run(conn, settings, repo_root: Path, question: str, llm_provider=None) -> list[dict]:
    repo_context = RepoContext(source_type="local", owner_repo=None, commit_sha=None)
    return list(
        generate_answer_events(
            conn, settings, FakeEmbeddingProvider(), llm_provider or FakeLLMProvider(), repo_context,
            repo_root, question=question,
        )
    )


def _by_event(events: list[dict], name: str) -> list[dict]:
    return [json.loads(e["data"]) for e in events if e["event"] == name]


def _immediate_answer(text: str) -> FakeLLMProvider:
    """A FakeLLMProvider scripted to answer the agent's very first turn
    with no tool calls — stands in for a confident real model that
    already has everything it needs from the seed context."""
    return FakeLLMProvider(tool_responses=[ToolUseResponse(text=text, tool_calls=[])])


def test_dependencies_route_never_calls_llm_and_answers_fast(tmp_path: Path):
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    events = _run(conn, settings, repo_root, "What dependencies does this project use?")

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
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    events = _run(conn, settings, repo_root, "What API endpoints does this app expose?")

    done = _by_event(events, "done")[0]
    assert done["route"] == "endpoints"
    assert done["latency_ms"] < 1000

    answer = "".join(json.loads(e["data"])["text"] for e in events if e["event"] == "token")
    assert "GET /health" in answer
    assert "POST /login" in answer


def test_locate_route_resolves_exact_symbol_match(tmp_path: Path):
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    events = _run(conn, settings, repo_root, "Where is `UserService` defined?")

    done = _by_event(events, "done")[0]
    assert done["route"] == "locate"
    assert done["latency_ms"] < 1000

    citations = _by_event(events, "citations")[0]["citations"]
    assert any(c["path"] == "src/users/service.py" for c in citations)


def test_locate_route_falls_back_to_agent_when_no_symbol_matches(tmp_path: Path):
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    # No named-symbol candidate in this question will match anything in
    # `symbols` (extract_candidate_names strips stopwords down to
    # "password"/"hashing", neither of which is a real symbol name) — must
    # gracefully fall through to the agent path instead of erroring or
    # returning an empty structured answer.
    fake_llm = _immediate_answer("Hashing happens in `hash_password` [src/auth/auth.py:17-21].")
    events = _run(conn, settings, repo_root, "Where does password hashing happen?", fake_llm)

    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"
    statuses = _by_event(events, "status")
    assert any(s.get("stage") == "retrieving" for s in statuses)


def test_overview_route_answers_from_cached_summary_without_agent(tmp_path: Path):
    from app.enrich.summarizer import summarize_repo

    conn, settings, repo_root = _index_mini_repo(tmp_path)
    summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")

    # No tool_responses scripted at all — if this route touched the agent
    # path it would raise, proving the summary-cache path never calls the LLM.
    events = _run(conn, settings, repo_root, "What does this repo do?", FakeLLMProvider())

    done = _by_event(events, "done")[0]
    assert done["route"] == "overview"
    assert done["latency_ms"] < 1000
    error_events = _by_event(events, "error")
    assert error_events == []


def test_overview_route_falls_back_to_agent_when_no_summary_yet(tmp_path: Path):
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    # Deliberately skip summarize_repo() — no repo-level summary exists.
    fake_llm = _immediate_answer("This repo is a small fixture demonstrating multi-language parsing.")
    events = _run(conn, settings, repo_root, "What does this repo do?", fake_llm)

    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"


def test_explain_route_uses_agent_path(tmp_path: Path):
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    fake_llm = _immediate_answer("The login flow validates credentials via [src/auth/auth.py:17-21].")
    events = _run(conn, settings, repo_root, "How does the login flow work?", fake_llm)

    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"
    statuses = _by_event(events, "status")
    stages = {s.get("stage") for s in statuses}
    assert "retrieving" in stages
    assert "expanding" in stages
    assert "generating" in stages


def test_explain_route_streams_tool_events_when_agent_calls_a_tool(tmp_path: Path):
    from app.providers.llm import LLMUsage, ToolCall

    conn, settings, repo_root = _index_mini_repo(tmp_path)
    fake_llm = FakeLLMProvider(
        tool_responses=[
            ToolUseResponse(
                text="",
                tool_calls=[ToolCall(id="t1", name="get_definition", input={"symbol": "UserService"})],
                stop_reason="tool_use",
                usage=LLMUsage(10, 5),
            ),
            ToolUseResponse(
                text="`UserService` is defined in [src/users/service.py:21-44].",
                tool_calls=[],
                usage=LLMUsage(10, 5),
            ),
        ]
    )
    events = _run(conn, settings, repo_root, "Trace how UserService is used across the codebase", fake_llm)

    tool_events = _by_event(events, "tool")
    assert len(tool_events) == 1
    assert tool_events[0]["name"] == "get_definition"

    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"


def test_session_id_is_recorded_on_each_logged_turn(tmp_path: Path):
    conn, settings, repo_root = _index_mini_repo(tmp_path)

    _run(conn, settings, repo_root, "Where is `UserService` defined?", None)
    rows = [r["session_id"] for r in conn.execute("SELECT session_id FROM query_log")]
    assert rows == [None]  # no session_id passed -> logged as NULL

    repo_context = RepoContext(source_type="local", owner_repo=None, commit_sha=None)
    list(
        generate_answer_events(
            conn, settings, FakeEmbeddingProvider(), FakeLLMProvider(), repo_context, repo_root,
            question="What dependencies does this project use?", session_id="session-abc",
        )
    )
    rows = [
        r["session_id"]
        for r in conn.execute("SELECT session_id FROM query_log WHERE session_id IS NOT NULL")
    ]
    assert rows == ["session-abc"]


def test_followup_question_without_llm_key_falls_back_to_original_gracefully(tmp_path: Path):
    """No ANTHROPIC_API_KEY -> the session-rewrite LLM call can't run, so
    `_resolve_question` must fail open to the raw (pronoun-laden) question
    rather than crash the whole answer — same fail-open contract
    retrieval/session.py::rewrite_followup already guarantees in isolation."""
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    repo_context = RepoContext(source_type="local", owner_repo=None, commit_sha=None)

    # First turn, to give the session some history.
    list(
        generate_answer_events(
            conn, settings, FakeEmbeddingProvider(), FakeLLMProvider(), repo_context, repo_root,
            question="Where is `UserService` defined?", session_id="s1",
        )
    )

    fake_llm = _immediate_answer("Errors are handled by returning None on failure.")
    events = list(
        generate_answer_events(
            conn, settings, FakeEmbeddingProvider(), fake_llm, repo_context, repo_root,
            question="How does it handle errors?", session_id="s1",
        )
    )
    error_events = _by_event(events, "error")
    assert error_events == []
    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"


def test_no_anthropic_key_does_not_break_structured_routes(tmp_path: Path):
    """The whole point of structured routes: they must work with zero LLM
    access, including for the ambiguous questions that would otherwise need
    the router's LLM-fallback classifier."""
    settings = make_settings(tmp_path, allow_local_repos=True, anthropic_api_key=None)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    registry_conn = get_registry_connection(settings)
    repo_root = Path(
        registry_conn.execute("SELECT local_path FROM repos WHERE id = ?", (repo_id,)).fetchone()["local_path"]
    )

    events = _run(conn, settings, repo_root, "What dependencies does this project use?")
    done = _by_event(events, "done")[0]
    assert done["route"] == "dependencies"
    error_events = _by_event(events, "error")
    assert error_events == []


def test_unrouteable_question_without_llm_key_defaults_to_explain_and_errors_gracefully(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True, anthropic_api_key=None)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    registry_conn = get_registry_connection(settings)
    repo_root = Path(
        registry_conn.execute("SELECT local_path FROM repos WHERE id = ?", (repo_id,)).fetchone()["local_path"]
    )
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
            repo_root,
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
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the synthesis LLM")
