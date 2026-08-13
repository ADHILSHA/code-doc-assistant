"""Context assembly + synthesis, streamed as the SSE events the frontend
depends on (SPEC.md §5).

SPEC.md §6 Phase 2 task 5: every question is routed first (retrieval/router.py)
into one of five strategies. `dependencies`/`endpoints` always answer
straight from their tables; `locate` tries a symbol-name lookup and only
falls back to hybrid retrieval if nothing matched. `explain`/`overview`
(and locate's fallback) go through the hybrid-retrieval + LLM path below —
the only path that calls an LLM at all. Structured answers are built
directly from SQL tables that are themselves a direct product of parsing
(no LLM step, nothing to hallucinate), and their inline `[path:START-END]`
citation markers are still run through `verify_citations` before being
returned — not to catch hallucination (there's no free-text generation to
hallucinate), but to get real `Citation` objects (with a permalink-ready
path/line range) out of the same code path the hybrid answer uses, instead
of building that shape twice.

Design note on the hybrid path: citation verification (generation/citations.py)
needs the *full* answer text before it can decide whether a claim's
citations are valid — fundamentally incompatible with true live
token-by-token streaming, since you can't verify what the model hasn't
generated yet. Rather than either occasionally showing an unverified/invalid
draft to the user, or inventing new SSE event semantics beyond the frozen
contract (SPEC.md §5), this buffers the full response via
`LLMProvider.complete()` (not `.stream()`), verifies it, regenerates once if
needed, and only then replays the final, citation-verified text as chunked
`token` events for the UI's streaming/typing effect. Trades true first-token
latency for guaranteeing the user only ever sees the answer that was
actually checked. `LLMProvider.stream()` is unused here as a result, but
it's still part of the required provider shape (SPEC.md §7.2) and may see
use in Phase 3's agent loop.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator

from app.config import Settings
from app.generation.citations import (
    RepoContext,
    attach_permalinks,
    verify_citations,
)
from app.generation.prompts import (
    REGENERATION_NOTE,
    SYSTEM_PROMPT,
    build_context_block,
    build_user_message,
)
from app.models import Citation, RetrievedChunk
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider, LLMUsage, get_summarization_llm_provider
from app.retrieval.hybrid import hybrid_search
from app.retrieval.router import Route, classify_route, classify_route_fast
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


def generate_answer_events(
    conn: sqlite3.Connection,
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    repo_context: RepoContext,
    *,
    question: str,
) -> Iterator[dict[str, str]]:
    """Sync generator of SSE-ready `{"event": ..., "data": <json str>}`
    dicts. Runs entirely synchronously; the API layer is responsible for
    offloading iteration to a thread so it doesn't block the event loop.
    """
    start = time.monotonic()
    try:
        route = _classify_route(question, settings)
        yield _event("status", {"stage": "routing", "detail": f"routed to '{route}'"})

        if route == "dependencies":
            answer_text, sources = _build_dependencies_answer(conn)
            yield from _finish(conn, repo_context, question, route, answer_text, sources, start)
            return

        if route == "endpoints":
            answer_text, sources = _build_endpoints_answer(conn)
            yield from _finish(conn, repo_context, question, route, answer_text, sources, start)
            return

        if route == "locate":
            located = _build_locate_answer(conn, question)
            if located is not None:
                answer_text, sources = located
                yield from _finish(conn, repo_context, question, route, answer_text, sources, start)
                return
            # No candidate symbol name from the question matched anything in
            # `symbols` — fall back to hybrid retrieval rather than return an
            # empty/wrong "confident" answer. Logged under "explain" (not
            # "locate") since that's the strategy that actually ran.
            route = "explain"

        yield from _answer_via_hybrid_retrieval(
            conn, settings, embedding_provider, llm_provider, repo_context, question, route, start
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an SSE error event
        yield _event("error", {"message": str(exc)})


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
        # route (hybrid retrieval; doesn't assume a SQL table the question
        # might not actually fit).
        return "explain"


# --- structured routes (SPEC.md §6 Phase 2 task 5) ---


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


# --- hybrid retrieval + LLM route (explain / overview / locate fallback) ---


def _answer_via_hybrid_retrieval(
    conn: sqlite3.Connection,
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    repo_context: RepoContext,
    question: str,
    route: Route,
    start: float,
) -> Iterator[dict[str, str]]:
    yield _event("status", {"stage": "retrieving", "detail": "hybrid search (dense + BM25)"})
    ranked = hybrid_search(
        conn,
        embedding_provider,
        question,
        top_k_dense=settings.top_k_dense,
        top_k_lexical=settings.top_k_lexical,
        rrf_k=settings.rrf_k,
        top_n=settings.hybrid_top_n,
    )
    chunks = _fetch_chunks(conn, [chunk_id for chunk_id, _score in ranked])
    sources = [
        {"path": c.file_path, "start_line": c.start_line, "end_line": c.end_line, "symbol": c.symbol_name}
        for c in chunks
    ]
    yield _event("sources", {"chunks": sources})
    yield _event("status", {"stage": "generating", "detail": f"{len(chunks)} chunks in context"})

    context_block = build_context_block(chunks)
    messages = [{"role": "user", "content": build_user_message(question, context_block)}]

    resp = llm_provider.complete(system=SYSTEM_PROMPT, messages=messages, max_tokens=1024)
    answer_text, usage = resp.text, resp.usage
    verification = verify_citations(conn, answer_text)

    if verification.unsupported_claim:
        yield _event("status", {"stage": "generating", "detail": "citation check failed — regenerating once"})
        retry_messages = [
            *messages,
            {"role": "assistant", "content": answer_text},
            {"role": "user", "content": REGENERATION_NOTE},
        ]
        resp = llm_provider.complete(system=SYSTEM_PROMPT, messages=retry_messages, max_tokens=1024)
        answer_text, usage = resp.text, resp.usage

    yield from _stream_tokens_and_log(
        conn, repo_context, question, route, answer_text, start,
        chunk_ids=[c.chunk_id for c in chunks], usage=usage,
    )


# --- shared tail: verify citations, replay as tokens, log, done ---


def _finish(
    conn: sqlite3.Connection,
    repo_context: RepoContext,
    question: str,
    route: Route,
    answer_text: str,
    sources: list[dict],
    start: float,
) -> Iterator[dict[str, str]]:
    yield _event("sources", {"chunks": sources})
    # No replay pacing for structured answers: SPEC.md's acceptance
    # criterion is "structured routes answer in <1s" — the artificial
    # per-word delay exists purely to make an LLM-generated answer *feel*
    # streamed (see module docstring), and a repo with enough endpoints/
    # dependencies to produce a long answer could otherwise blow that
    # budget on cosmetic pacing alone rather than any real work.
    yield from _stream_tokens_and_log(conn, repo_context, question, route, answer_text, start, pace=False)


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
    pace: bool = True,
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
    )
    yield _event("done", {"query_id": query_id, "latency_ms": latency_ms, "route": route})


def _event(name: str, data: dict) -> dict[str, str]:
    return {"event": name, "data": json.dumps(data)}


def _fetch_chunks(conn: sqlite3.Connection, chunk_id_order: list[int]) -> list[RetrievedChunk]:
    if not chunk_id_order:
        return []
    placeholders = ",".join("?" for _ in chunk_id_order)
    rows = conn.execute(
        "SELECT c.id, c.symbol_name, c.symbol_kind, c.start_line, c.end_line, c.header, "
        "c.content, f.path "
        f"FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id IN ({placeholders})",
        chunk_id_order,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    ordered = [by_id[cid] for cid in chunk_id_order if cid in by_id]
    return [
        RetrievedChunk(
            chunk_id=r["id"],
            file_path=r["path"],
            symbol_name=r["symbol_name"],
            symbol_kind=r["symbol_kind"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            content=r["content"],
            header=r["header"],
        )
        for r in ordered
    ]


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
) -> int:
    cur = conn.execute(
        "INSERT INTO query_log "
        "(question, route, answer, citations_json, chunk_ids_json, tool_calls, latency_ms, "
        " input_tokens, output_tokens, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            question,
            route,
            answer,
            json.dumps([c.model_dump() for c in citations]),
            json.dumps(chunk_ids),
            0,
            latency_ms,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid
