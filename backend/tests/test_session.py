from __future__ import annotations

from pathlib import Path

from app.db import get_repo_connection
from app.providers.llm import FakeLLMProvider
from app.retrieval.session import get_recent_turns, rewrite_followup

from .conftest import make_settings


def _log_turn(conn, session_id: str, question: str, answer: str) -> None:
    conn.execute(
        "INSERT INTO query_log (question, route, answer, session_id, created_at) "
        "VALUES (?, 'explain', ?, ?, datetime('now'))",
        (question, answer, session_id),
    )
    conn.commit()


def test_get_recent_turns_returns_oldest_first_within_limit(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("r1", 32, settings)
    _log_turn(conn, "s1", "q1", "a1")
    _log_turn(conn, "s1", "q2", "a2")
    _log_turn(conn, "s1", "q3", "a3")

    turns = get_recent_turns(conn, "s1", limit=2)
    assert turns == [("q2", "a2"), ("q3", "a3")]


def test_get_recent_turns_ignores_other_sessions(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("r1", 32, settings)
    _log_turn(conn, "s1", "q1", "a1")
    _log_turn(conn, "s2", "other question", "other answer")

    turns = get_recent_turns(conn, "s1", limit=3)
    assert turns == [("q1", "a1")]


def test_get_recent_turns_no_session_id_returns_empty(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("r1", 32, settings)
    _log_turn(conn, "s1", "q1", "a1")
    assert get_recent_turns(conn, None, limit=3) == []


def test_get_recent_turns_unknown_session_returns_empty(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("r1", 32, settings)
    assert get_recent_turns(conn, "no-such-session", limit=3) == []


def test_rewrite_followup_passes_through_without_anaphora():
    history = [("Where is UserService defined?", "In src/users/service.py.")]
    result = rewrite_followup(FakeLLMProvider(responses=[]), "What does the repo do?", history)
    assert result == "What does the repo do?"


def test_rewrite_followup_passes_through_without_history():
    result = rewrite_followup(FakeLLMProvider(responses=[]), "how does it handle errors?", [])
    assert result == "how does it handle errors?"


def test_rewrite_followup_resolves_pronoun_via_llm():
    history = [("Where is UserService defined?", "In src/users/service.py.")]
    fake = FakeLLMProvider(responses=["How does UserService handle errors?"])
    result = rewrite_followup(fake, "how does it handle errors?", history)
    assert result == "How does UserService handle errors?"


def test_rewrite_followup_fails_open_on_llm_error():
    class _RaisingProvider:
        model = "raising"

        def complete(self, **kwargs):
            raise RuntimeError("boom")

    history = [("Where is UserService defined?", "In src/users/service.py.")]
    result = rewrite_followup(_RaisingProvider(), "how does it work?", history)
    assert result == "how does it work?"


def test_rewrite_followup_falls_back_to_original_on_blank_llm_response():
    history = [("Where is UserService defined?", "In src/users/service.py.")]
    fake = FakeLLMProvider(responses=["   "])
    result = rewrite_followup(fake, "how does it work?", history)
    assert result == "how does it work?"
