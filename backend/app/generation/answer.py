"""Context assembly + synthesis, streamed as the SSE events the frontend
depends on (SPEC.md §5).

Design note: citation verification (generation/citations.py) needs the
*full* answer text before it can decide whether a claim's citations are
valid — fundamentally incompatible with true live token-by-token
streaming, since you can't verify what the model hasn't generated yet.
Rather than either occasionally showing an unverified/invalid draft to the
user, or inventing new SSE event semantics beyond the frozen contract
(SPEC.md §5), this buffers the full response via `LLMProvider.complete()`
(not `.stream()`), verifies it, regenerates once if needed, and only then
replays the final, citation-verified text as chunked `token` events for
the UI's streaming/typing effect. Trades true first-token latency for
guaranteeing the user only ever sees the answer that was actually checked.
`LLMProvider.stream()` is unused here as a result, but it's still part of
the required provider shape (SPEC.md §7.2) and may see use in Phase 3's
agent loop.
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
from app.providers.llm import LLMProvider, LLMUsage
from app.retrieval.hybrid import hybrid_search

# Small artificial pacing between replayed "token" events so the UI still
# reads as a stream even though the underlying LLM call wasn't (see module
# docstring) — cosmetic only, not worth a config field.
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

        yield _event(
            "sources",
            {
                "chunks": [
                    {
                        "path": c.file_path,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "symbol": c.symbol_name,
                    }
                    for c in chunks
                ]
            },
        )
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
            verification = verify_citations(conn, answer_text)

        for word in answer_text.split(" "):
            yield _event("token", {"text": word + " "})
            if _TOKEN_REPLAY_DELAY_SECONDS:
                time.sleep(_TOKEN_REPLAY_DELAY_SECONDS)

        citations = attach_permalinks(verification.citations, repo_context)
        yield _event("citations", {"citations": [c.model_dump() for c in citations]})

        latency_ms = int((time.monotonic() - start) * 1000)
        query_id = _log_query(
            conn,
            question=question,
            route="naive",
            answer=answer_text,
            citations=citations,
            chunk_ids=[c.chunk_id for c in chunks],
            latency_ms=latency_ms,
            usage=usage,
        )
        yield _event("done", {"query_id": query_id, "latency_ms": latency_ms, "route": "naive"})
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an SSE error event
        yield _event("error", {"message": str(exc)})


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
