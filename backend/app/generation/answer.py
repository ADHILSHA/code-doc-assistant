"""Context assembly + synthesis, streamed as the SSE events the frontend
depends on (SPEC.md §5).

Phase 0 note: citation extraction here is a lightweight regex parse of
whatever the model claims — it is NOT yet verified against real file/line
data. That's Phase 1's `generation/citations.py` (parse + VERIFY citations,
strip hallucinated ones, regenerate once on failure). Likewise `route` is
hardcoded to "naive" since the query router doesn't exist until Phase 2.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterator

from app.config import Settings
from app.generation.prompts import SYSTEM_PROMPT, build_context_block, build_user_message
from app.index.vectors import query_top_k
from app.models import Citation, RetrievedChunk
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider, LLMUsage

_CITATION_RE = re.compile(r"\[([\w./-]+):(\d+)-(\d+)\]")


def generate_answer_events(
    conn: sqlite3.Connection,
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    llm_provider: LLMProvider,
    *,
    question: str,
) -> Iterator[dict[str, str]]:
    """Sync generator of SSE-ready `{"event": ..., "data": <json str>}`
    dicts. Runs entirely synchronously; the API layer is responsible for
    offloading iteration to a thread so it doesn't block the event loop.
    """
    start = time.monotonic()
    try:
        yield _event("status", {"stage": "retrieving", "detail": "dense search"})
        top = query_top_k(conn, embedding_provider, question, settings.top_k_dense)
        chunks = _fetch_chunks(conn, [chunk_id for chunk_id, _ in top])

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

        answer_text = ""
        usage: LLMUsage | None = None
        for item in llm_provider.stream(system=SYSTEM_PROMPT, messages=messages, max_tokens=1024):
            if isinstance(item, LLMUsage):
                usage = item
            else:
                answer_text += item
                yield _event("token", {"text": item})

        citations = _extract_citations(answer_text)
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


def _extract_citations(text: str) -> list[Citation]:
    citations = []
    for i, m in enumerate(_CITATION_RE.finditer(text), start=1):
        path, start_line, end_line = m.group(1), int(m.group(2)), int(m.group(3))
        citations.append(Citation(id=i, path=path, start_line=start_line, end_line=end_line))
    return citations


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
