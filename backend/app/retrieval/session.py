"""Session memory (SPEC.md §6 Phase 3 task 5): rewrite a follow-up
question against the last few turns of the same session, so pronouns
resolve ("how does *it* handle errors?") before the question ever reaches
the router or retrieval — everything downstream (routing, hybrid search,
expansion, the agent) only ever sees a self-contained question and has no
idea a rewrite happened.
"""

from __future__ import annotations

import re
import sqlite3

from app.providers.llm import LLMProvider
from app.retrieval.session_prompts import (
    SESSION_REWRITE_SYSTEM_PROMPT,
    build_session_rewrite_user_message,
)

# Anaphora markers — if none of these appear, the question is almost
# certainly already self-contained, so skip the LLM call entirely (same
# "cheap regex fast-path, LLM only when needed" shape as retrieval/router.py
# — most questions in a session are new topics, not follow-ups, and an LLM
# round-trip on every single one would be pure waste).
_FOLLOWUP_MARKER_RE = re.compile(
    r"\b(it|its|it's|this|that|these|those|they|them|their|"
    r"the same|that one|the above|the previous one)\b",
    re.IGNORECASE,
)


def get_recent_turns(
    conn: sqlite3.Connection, session_id: str | None, *, limit: int
) -> list[tuple[str, str]]:
    """Oldest-first (question, answer) pairs from the last `limit` turns of
    this session. [] for no session id, or a session with no prior turns."""
    if not session_id:
        return []
    rows = conn.execute(
        "SELECT question, answer FROM query_log WHERE session_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [(r["question"], r["answer"] or "") for r in reversed(rows)]


def rewrite_followup(
    llm_provider: LLMProvider, question: str, history: list[tuple[str, str]]
) -> str:
    """`question` verbatim if there's no history, the question doesn't
    look like a follow-up, or the LLM call itself fails — fails open to
    the original question rather than risk mangling a perfectly fine
    standalone one into something worse."""
    if not history or not _FOLLOWUP_MARKER_RE.search(question):
        return question
    try:
        resp = llm_provider.complete(
            system=SESSION_REWRITE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_session_rewrite_user_message(question, history)}],
            max_tokens=200,
        )
    except Exception:  # noqa: BLE001 - a rewrite failure must not fail the whole answer
        return question
    rewritten = resp.text.strip()
    return rewritten or question
