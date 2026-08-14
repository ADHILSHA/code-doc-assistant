"""Context assembly + synthesis, streamed as the SSE events the frontend
depends on (SPEC.md §5).

SPEC.md §6 Phase 2 task 5 / Phase 3: every question is first rewritten
against session history if it looks like a follow-up (retrieval/session.py),
then routed (retrieval/router.py) into one of five strategies:
- `dependencies`/`endpoints` always answer straight from their tables.
- `locate` tries a symbol-name lookup; `overview` tries the cached
  repo-level summary (Phase 3 task 4) — both fall back to the agent path
  below if nothing structured is available yet.
- `explain` (and the two fallbacks above) go through hybrid retrieval ->
  graph expansion -> reranking -> the agent tool-use loop (Phase 3 tasks
  1-3) — the only path that calls an LLM at all.

Structured answers are built directly from SQL tables that are themselves a
direct product of parsing (no LLM step, nothing to hallucinate), and their
inline `[path:START-END]` citation markers are still run through
`verify_citations` before being returned — not to catch hallucination
(there's no free-text generation to hallucinate), but to get real
`Citation` objects (with a permalink-ready path/line range) out of the same
code path the agent answer uses, instead of building that shape twice.

Design note on the agent path: citation verification (generation/citations.py)
needs the *full* answer text before it can decide whether a claim's
citations are valid — fundamentally incompatible with true live
token-by-token streaming, since you can't verify what the model hasn't
generated yet. Rather than either occasionally showing an unverified/invalid
draft to the user, or inventing new SSE event semantics beyond the frozen
contract (SPEC.md §5), this runs the full agent loop to completion, verifies
the final text, and only then replays it as chunked `token` events for the
UI's streaming/typing effect. Trades true first-token latency for
guaranteeing the user only ever sees the answer that was actually checked.
Unlike Phase 1-2's flat hybrid-retrieval path, the agent path does NOT
retry-once on an unsupported claim — re-running a whole multi-turn tool-use
loop for one retry was judged not worth the added cost/latency; an invalid
citation is still stripped by `verify_citations`, just without a second
attempt at a fully-cited answer (see DECISIONS.md).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from app.agent.loop import AgentDone, AgentResult, AgentToolEvent, run_agent
from app.agent.tools import ToolContext
from app.config import Settings
from app.generation.citations import (
    RepoContext,
    attach_permalinks,
    verify_citations,
)
from app.logging_setup import get_logger
from app.models import Citation, RetrievedChunk
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider, LLMUsage, get_summarization_llm_provider
from app.retrieval.chunks import fetch_chunks
from app.retrieval.expand import expand_context
from app.retrieval.hybrid import hybrid_search
from app.retrieval.rerank import merge_adjacent, rerank
from app.retrieval.router import Route, classify_route, classify_route_fast
from app.retrieval.session import get_recent_turns, rewrite_followup
from app.retrieval.structured import (
    DependencyRow,
    extract_candidate_names,
    get_definition,
    list_dependencies,
    list_endpoints,
)

# Small artificial pacing between replayed "token" events so the UI still
# reads as a stream even though the underlying LLM call wasn't (see module
# docstring) — cosmetic only, not worth a config field. Structured answers
# reuse the same replay for a consistent UI feel across routes.
_TOKEN_REPLAY_DELAY_SECONDS = 0.015

logger = get_logger(__name__)


def _user_facing_error_message(exc: Exception) -> str:
    """SPEC.md §6 Phase 5 task 1 (error states): the SSE `error` event's
    `message` reaches the chat UI verbatim (MessageBubble renders it as-is)
    — found via a live smoke test that an unwrapped provider exception's
    `str()` is raw SDK/HTTP internals (e.g. `"Error code: 400 - {'type':
    'error', 'error': {...}, 'request_id': '...'}"`  from a real
    insufficient-credits response), not something a non-technical user
    should have to parse. The real exception is always logged in full
    server-side (with this request's `request_id` trace, via
    logging_setup.py) either way — this only controls what the *client*
    sees.

    Duck-typed on `anthropic` exception class names rather than importing
    `anthropic` at module load time, matching the lazy-import-as-optional-
    dependency pattern `providers/llm.py` already uses for the same
    reason: this module has no other reason to depend on any specific
    LLM vendor's SDK.
    """
    type_name = type(exc).__name__
    module_name = type(exc).__module__
    if module_name.startswith("anthropic"):
        if type_name == "AuthenticationError":
            return "The configured Anthropic API key is invalid. Contact whoever runs this deployment."
        if type_name == "RateLimitError":
            return "The AI provider is rate-limiting requests right now. Please try again in a moment."
        if type_name in ("APIConnectionError", "APITimeoutError"):
            return "Could not reach the AI provider. Please try again in a moment."
        if type_name == "BadRequestError" and "credit balance" in str(exc).lower():
            return "The configured Anthropic API key has no available credit. Contact whoever runs this deployment."
        if type_name in ("OverloadedError", "InternalServerError"):
            return "The AI provider is temporarily unavailable. Please try again in a moment."
        return "The AI provider rejected the request. Contact whoever runs this deployment if this persists."
    return "Something went wrong while generating the answer. Please try again."


def generate_answer_events(
    conn: sqlite3.Connection,
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    repo_context: RepoContext,
    repo_root: Path,
    *,
    question: str,
    session_id: str | None = None,
) -> Iterator[dict[str, str]]:
    """Sync generator of SSE-ready `{"event": ..., "data": <json str>}`
    dicts. Runs entirely synchronously; the API layer is responsible for
    offloading iteration to a thread so it doesn't block the event loop.

    `repo_root` is the repo's cloned/local working tree on disk — needed
    now (Phase 3) because the agent's `read_file`/`grep`/`find_files` tools
    read from it, path-jailed via ingest/safe_path.py.
    """
    start = time.monotonic()
    try:
        question = _resolve_question(conn, settings, question, session_id)
        route = _classify_route(question, settings)
        yield _event("status", {"stage": "routing", "detail": f"routed to '{route}'"})

        if route == "dependencies":
            answer_text, sources = _build_dependencies_answer(conn)
            yield from _finish(conn, repo_context, question, route, answer_text, sources, start, session_id)
            return

        if route == "endpoints":
            answer_text, sources = _build_endpoints_answer(conn)
            yield from _finish(conn, repo_context, question, route, answer_text, sources, start, session_id)
            return

        if route == "locate":
            located = _build_locate_answer(conn, question)
            if located is not None:
                answer_text, sources = located
                yield from _finish(conn, repo_context, question, route, answer_text, sources, start, session_id)
                return
            # No candidate symbol name from the question matched anything in
            # `symbols` — fall back to the agent path rather than return an
            # empty/wrong "confident" answer. Logged under "explain" (not
            # "locate") since that's the strategy that actually ran.
            route = "explain"

        if route == "overview":
            overview = _build_overview_answer(conn)
            if overview is not None:
                answer_text, sources = overview
                yield from _finish(conn, repo_context, question, route, answer_text, sources, start, session_id)
                return
            # No repo-level summary yet (repo indexed before summarization
            # ran, or summarization was skipped for lack of an LLM key) —
            # fall back to the agent path instead of answering nothing.
            route = "explain"

        yield from _answer_via_agent(
            conn, settings, embedding_provider, llm_provider, repo_context, repo_root, question, route,
            start, session_id,
        )
    except Exception as exc:
        logger.exception("query failed", extra={"question": question})
        yield _event("error", {"message": _user_facing_error_message(exc)})


def _resolve_question(
    conn: sqlite3.Connection, settings: Settings, question: str, session_id: str | None
) -> str:
    """SPEC.md §6 Phase 3 task 5: resolve pronouns/implicit references
    against the last few turns of this session before anything downstream
    (routing, retrieval, the agent) ever sees the question."""
    if not session_id:
        return question
    history = get_recent_turns(conn, session_id, limit=settings.session_history_turns)
    if not history:
        return question
    try:
        rewrite_llm = get_summarization_llm_provider(settings)
    except Exception:  # noqa: BLE001 - a rewrite failure must not fail the whole answer
        return question
    return rewrite_followup(rewrite_llm, question, history)


def _classify_route(question: str, settings: Settings) -> Route:
    fast = classify_route_fast(question)
    if fast is not None:
        return fast
    try:
        router_llm = get_summarization_llm_provider(settings)
        return classify_route(question, router_llm)
    except Exception:  # noqa: BLE001 - classification failure shouldn't fail the whole answer
        # No summarization-capable provider available (e.g. no
        # ANTHROPIC_API_KEY in a fake-embeddings demo) or the classification
        # call itself failed — default to "explain", the safest generic
        # route (agent path; doesn't assume a SQL table the question might
        # not actually fit).
        return "explain"


# --- structured routes (SPEC.md §6 Phase 2 task 5 / Phase 3 task 4) ---


def _build_dependencies_answer(conn: sqlite3.Connection) -> tuple[str, list[dict]]:
    deps = list_dependencies(conn)
    if not deps:
        message = (
            "No dependency manifest (package.json, pyproject.toml, requirements.txt, ...) "
            "was found in this repo."
        )
        return message, []

    by_manifest: dict[str, list[DependencyRow]] = {}
    for d in deps:
        by_manifest.setdefault(d.manifest_path, []).append(d)

    lines: list[str] = []
    sources: list[dict] = []
    for manifest_path in sorted(by_manifest):
        rows = sorted(by_manifest[manifest_path], key=lambda r: r.name)
        names = ", ".join(f"{r.name} ({r.version_spec})" if r.version_spec else r.name for r in rows)
        lines.append(f"**{manifest_path}** [{manifest_path}:1-1] — {len(rows)} dependencies: {names}")
        sources.append({"path": manifest_path, "start_line": 1, "end_line": 1, "symbol": None})
    return "\n\n".join(lines), sources


def _build_endpoints_answer(conn: sqlite3.Connection) -> tuple[str, list[dict]]:
    endpoints = list_endpoints(conn)
    if not endpoints:
        return "No API endpoints were found in this repo.", []

    lines: list[str] = []
    sources: list[dict] = []
    for e in endpoints:
        method = e.method or "ANY"
        handler = f" -> `{e.handler_symbol}`" if e.handler_symbol else ""
        auth = f" (requires auth: {e.auth_hint})" if e.auth_hint else ""
        if e.file_path and e.line:
            cite = f" [{e.file_path}:{e.line}-{e.line}]"
            sources.append({"path": e.file_path, "start_line": e.line, "end_line": e.line, "symbol": e.handler_symbol})
        else:
            cite = ""
        lines.append(f"- `{method} {e.route}`{handler}{auth}{cite}")

    header = f"This repo exposes {len(endpoints)} endpoint(s):\n\n"
    return header + "\n".join(lines), sources


def _build_locate_answer(conn: sqlite3.Connection, question: str) -> tuple[str, list[dict]] | None:
    for name in extract_candidate_names(question):
        matches = get_definition(conn, name)
        if not matches:
            continue
        lines: list[str] = []
        sources: list[dict] = []
        for m in matches:
            doc = f" — {m.docstring.splitlines()[0]}" if m.docstring else ""
            lines.append(f"`{m.name}` ({m.kind}) is defined in [{m.file_path}:{m.start_line}-{m.end_line}]{doc}")
            sources.append(
                {"path": m.file_path, "start_line": m.start_line, "end_line": m.end_line, "symbol": m.name}
            )
        return "\n".join(lines), sources
    return None


def _build_overview_answer(conn: sqlite3.Connection) -> tuple[str, list[dict]] | None:
    """SPEC.md §6 Phase 3 acceptance criterion: "Repo-overview question
    answers correctly using summaries without reading source files." None
    if no repo-level summary has been generated yet — the caller falls
    back to the agent path rather than answer nothing."""
    row = conn.execute(
        "SELECT content FROM summaries WHERE scope = 'repo' AND target_path = '.'"
    ).fetchone()
    if row is None:
        return None
    return row["content"], []


# --- agent path: hybrid retrieval -> graph expansion -> rerank -> agent loop ---
# (explain / overview fallback / locate fallback — SPEC.md §6 Phase 3 tasks 1-3)


def _answer_via_agent(
    conn: sqlite3.Connection,
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    repo_context: RepoContext,
    repo_root: Path,
    question: str,
    route: Route,
    start: float,
    session_id: str | None,
) -> Iterator[dict[str, str]]:
    yield _event("status", {"stage": "retrieving", "detail": "hybrid search (dense + BM25)"})
    ranked = hybrid_search(
        conn, embedding_provider, question,
        top_k_dense=settings.top_k_dense, top_k_lexical=settings.top_k_lexical,
        rrf_k=settings.rrf_k, top_n=settings.hybrid_top_n,
    )
    seed_ids = [chunk_id for chunk_id, _score in ranked]

    yield _event("status", {"stage": "expanding", "detail": "graph expansion (definitions, callers, imports)"})
    expanded_ids = expand_context(
        conn, seed_ids, max_hops=settings.expand_max_hops, token_budget=settings.expand_token_budget
    )
    all_ids = seed_ids + [cid for cid in expanded_ids if cid not in seed_ids]
    candidates = fetch_chunks(conn, all_ids)

    yield _event("status", {"stage": "generating", "detail": f"reranking {len(candidates)} candidate(s)"})
    top_candidates = _rerank_candidates(settings, question, candidates)
    seed_chunks = merge_adjacent(top_candidates)

    sources = [
        {"path": c.file_path, "start_line": c.start_line, "end_line": c.end_line, "symbol": c.symbol_name}
        for c in seed_chunks
    ]
    yield _event("sources", {"chunks": sources})
    yield _event(
        "status", {"stage": "generating", "detail": f"agent investigating with {len(seed_chunks)} seed chunk(s)"}
    )

    tool_ctx = ToolContext(
        conn=conn, repo_root=repo_root, embedding_provider=embedding_provider, settings=settings
    )
    agent_result: AgentResult | None = None
    for event in run_agent(
        llm_provider, tool_ctx, question, seed_chunks,
        max_iterations=settings.agent_max_iterations,
        max_context_tokens=settings.agent_max_context_tokens,
        max_wall_seconds=settings.agent_max_wall_seconds,
    ):
        if isinstance(event, AgentToolEvent):
            yield _event(
                "tool",
                {
                    "name": event.record.name,
                    "input": event.record.input,
                    "result_summary": event.record.result_summary,
                },
            )
            yield _event("status", {"stage": "tool_call", "detail": event.record.result_summary})
        elif isinstance(event, AgentDone):
            agent_result = event.result

    assert agent_result is not None  # run_agent always yields exactly one AgentDone

    yield from _stream_tokens_and_log(
        conn, repo_context, question, route, agent_result.text, start,
        chunk_ids=[c.chunk_id for c in seed_chunks], usage=agent_result.usage,
        tool_calls=len(agent_result.tool_calls), session_id=session_id,
    )


def _rerank_candidates(
    settings: Settings, question: str, candidates: list[RetrievedChunk]
) -> list[RetrievedChunk]:
    if len(candidates) <= settings.rerank_keep_n:
        return candidates
    try:
        rerank_llm = get_summarization_llm_provider(settings)
    except Exception:  # noqa: BLE001 - reranking is a quality optimization, not required for correctness
        return candidates[: settings.rerank_keep_n]
    return rerank(rerank_llm, question, candidates, keep_n=settings.rerank_keep_n)


# --- shared tail: verify citations, replay as tokens, log, done ---


def _finish(
    conn: sqlite3.Connection,
    repo_context: RepoContext,
    question: str,
    route: Route,
    answer_text: str,
    sources: list[dict],
    start: float,
    session_id: str | None,
) -> Iterator[dict[str, str]]:
    yield _event("sources", {"chunks": sources})
    # No replay pacing for structured answers: SPEC.md's acceptance
    # criterion is "structured routes answer in <1s" — the artificial
    # per-word delay exists purely to make an LLM-generated answer *feel*
    # streamed (see module docstring), and a repo with enough endpoints/
    # dependencies to produce a long answer could otherwise blow that
    # budget on cosmetic pacing alone rather than any real work.
    yield from _stream_tokens_and_log(
        conn, repo_context, question, route, answer_text, start, pace=False, session_id=session_id
    )


def _stream_tokens_and_log(
    conn: sqlite3.Connection,
    repo_context: RepoContext,
    question: str,
    route: Route,
    answer_text: str,
    start: float,
    *,
    chunk_ids: list[int] | None = None,
    usage: LLMUsage | None = None,
    tool_calls: int = 0,
    pace: bool = True,
    session_id: str | None = None,
) -> Iterator[dict[str, str]]:
    verification = verify_citations(conn, answer_text)

    for word in answer_text.split(" "):
        yield _event("token", {"text": word + " "})
        if pace and _TOKEN_REPLAY_DELAY_SECONDS:
            time.sleep(_TOKEN_REPLAY_DELAY_SECONDS)

    citations = attach_permalinks(verification.citations, repo_context)
    yield _event("citations", {"citations": [c.model_dump() for c in citations]})

    latency_ms = int((time.monotonic() - start) * 1000)
    query_id = _log_query(
        conn,
        question=question,
        route=route,
        answer=answer_text,
        citations=citations,
        chunk_ids=chunk_ids or [],
        latency_ms=latency_ms,
        usage=usage,
        tool_calls=tool_calls,
        session_id=session_id,
    )
    yield _event("done", {"query_id": query_id, "latency_ms": latency_ms, "route": route})


def _event(name: str, data: dict) -> dict[str, str]:
    return {"event": name, "data": json.dumps(data)}


def _log_query(
    conn: sqlite3.Connection,
    *,
    question: str,
    route: str,
    answer: str,
    citations: list[Citation],
    chunk_ids: list[int],
    latency_ms: int,
    usage: LLMUsage | None,
    tool_calls: int = 0,
    session_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO query_log "
        "(question, route, answer, citations_json, chunk_ids_json, tool_calls, latency_ms, "
        " input_tokens, output_tokens, session_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            question,
            route,
            answer,
            json.dumps([c.model_dump() for c in citations]),
            json.dumps(chunk_ids),
            tool_calls,
            latency_ms,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            session_id,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid
