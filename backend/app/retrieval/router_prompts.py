"""Named prompt constants for query routing (SPEC.md §7.6)."""

from __future__ import annotations

ROUTER_SYSTEM_PROMPT = """You are a query router for a code documentation assistant. Classify the \
user's question into EXACTLY ONE of these five routes, and respond with ONLY that one word — no \
punctuation, no explanation:

dependencies - the question asks what libraries/packages/dependencies the codebase uses
endpoints    - the question asks what API endpoints/routes the codebase exposes
locate       - the question asks WHERE something is defined, or WHICH file/module handles something
explain      - the question asks HOW something works, or to TRACE a flow through the code
overview     - the question asks for a general summary of what the repo/project does

Respond with exactly one of: dependencies, endpoints, locate, explain, overview"""


def build_router_user_message(question: str) -> str:
    return f"Question: {question}"
