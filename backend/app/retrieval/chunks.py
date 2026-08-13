"""Shared chunk-hydration helper: turn a list of `chunks.id` values into
`RetrievedChunk` objects, preserving the caller's ordering. Used by both
generation/answer.py's hybrid-retrieval path and agent/tools.py's
`semantic_search` tool — extracted here (Phase 3) instead of staying a
private copy in generation/answer.py once a second caller needed it.
"""

from __future__ import annotations

import sqlite3

from app.models import RetrievedChunk


def fetch_chunks(conn: sqlite3.Connection, chunk_id_order: list[int]) -> list[RetrievedChunk]:
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
