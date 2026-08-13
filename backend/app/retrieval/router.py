"""Query router (SPEC.md §6 Phase 2 task 5): classify each question into
one of five routes. Regex fast-paths catch the obvious phrasings; anything
they don't recognize falls back to a cheap-model classification call with
strict enum output (`get_summarization_llm_provider` — see providers/llm.py).
"""

from __future__ import annotations

import re

from app.providers.llm import LLMProvider
from app.retrieval.router_prompts import ROUTER_SYSTEM_PROMPT, build_router_user_message

Route = str  # "dependencies" | "endpoints" | "locate" | "explain" | "overview"

VALID_ROUTES = ("dependencies", "endpoints", "locate", "explain", "overview")

_DEPENDENCIES_RE = re.compile(
    r"\b(librar(?:y|ies)|packages?|dependenc(?:y|ies)|npm|pypi|gems?|crates?)\b", re.IGNORECASE
)
_ENDPOINTS_RE = re.compile(
    r"\b(endpoints?|routes?|api\s+(?:does|expose|surface|look)|rest\s+api|http\s+(?:api|endpoints?))\b",
    re.IGNORECASE,
)
_LOCATE_RE = re.compile(
    r"^\s*where\s+(?:is|are|does)\b|\bwhich\s+file\b|\bwhich\s+(?:module|class|function)\s+handles\b",
    re.IGNORECASE,
)
_OVERVIEW_RE = re.compile(
    r"\bwhat\s+does\s+this\s+(?:repo|project|codebase|application|app|service)\s+do\b"
    r"|\bwhat\s+is\s+this\s+(?:repo|project|codebase|application|app|service)\s+(?:for|about)\b"
    r"|\bgive\s+me\s+an?\s+overview\b"
    r"|\bsummar(?:y|ize)\b",
    re.IGNORECASE,
)
_EXPLAIN_RE = re.compile(r"\bhow\s+does\b|\bhow\s+is\b.*\bimplemented\b", re.IGNORECASE)
# "trace" / "walk me through" are unambiguous markers of an explain question
# even when the sentence also happens to contain a dependencies/endpoints
# cue word (e.g. "trace the request from route to database" contains
# "route", which _ENDPOINTS_RE would otherwise claim first) — found via the
# 20-question labeled smoke test, not by inspection. Checked before the
# other routes for that reason; the plain "how does"/"how is ... implemented"
# patterns stay last since those phrasings commonly co-occur with a more
# specific cue (e.g. "how does the dependency resolver work").
_EXPLAIN_STRONG_RE = re.compile(r"\btrace\b|\bwalk\s+me\s+through\b", re.IGNORECASE)

_FAST_PATH_ORDER: list[tuple[Route, re.Pattern[str]]] = [
    ("explain", _EXPLAIN_STRONG_RE),
    ("dependencies", _DEPENDENCIES_RE),
    ("endpoints", _ENDPOINTS_RE),
    ("locate", _LOCATE_RE),
    ("overview", _OVERVIEW_RE),
    ("explain", _EXPLAIN_RE),
]


def classify_route_fast(question: str) -> Route | None:
    """Regex fast-path. None if nothing matched confidently — the caller
    should fall back to the LLM classifier."""
    for route, pattern in _FAST_PATH_ORDER:
        if pattern.search(question):
            return route
    return None


def classify_route(question: str, llm_provider: LLMProvider) -> Route:
    """`llm_provider` should already be constructed against the cheap
    summarization model (`get_summarization_llm_provider`) — this module
    doesn't pick a model, it just uses whatever provider it's handed."""
    fast = classify_route_fast(question)
    if fast is not None:
        return fast
    return _classify_with_llm(question, llm_provider)


def _classify_with_llm(question: str, llm_provider: LLMProvider) -> Route:
    resp = llm_provider.complete(
        system=ROUTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_router_user_message(question)}],
        max_tokens=20,
    )
    text = resp.text.strip().lower()
    for route in VALID_ROUTES:
        if route in text:
            return route
    return "explain"  # safest generic fallback: hybrid retrieval, no SQL hallucination risk
