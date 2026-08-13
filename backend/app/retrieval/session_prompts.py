"""Named prompt constants for follow-up question rewriting (SPEC.md §7.6)."""

from __future__ import annotations

_ANSWER_CHARS = 300

SESSION_REWRITE_SYSTEM_PROMPT = """You resolve pronouns and implicit references in a follow-up question \
about a codebase, using the recent conversation as context. Rewrite the LATEST question into a fully \
self-contained question that means the same thing but doesn't rely on the conversation history to \
understand what "it"/"that"/"this"/"they" refers to. Output ONLY the rewritten question, nothing else \
— no preamble, no quotes around it. If the question is already self-contained, output it unchanged."""


def build_session_rewrite_user_message(question: str, history: list[tuple[str, str]]) -> str:
    parts = ["Recent conversation:"]
    for q, a in history:
        parts.append(f"Q: {q}\nA: {a[:_ANSWER_CHARS]}")
    parts.append(f"\nLatest question: {question}")
    return "\n\n".join(parts)
