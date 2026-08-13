"""FTS5 lexical index (BM25) — SPEC.md §6 Phase 1 task 2.

Identifiers are indexed both as written and split into natural-language
tokens (`getUserById` is also indexed as "get user by id"), so both an
exact-identifier query and a natural-language query hit the same row.
"""

from __future__ import annotations

import re
import sqlite3

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_identifier(name: str) -> str:
    """'getUserById' -> 'get user by id'; 'get_user_by_id' -> same;
    'HTTPServer' -> 'http server'."""
    s = re.sub(r"[_\-]+", " ", name)
    s = _CAMEL_BOUNDARY_RE.sub(" ", s)
    return " ".join(s.lower().split())


def index_chunk(
    conn: sqlite3.Connection, chunk_id: int, *, content: str, symbol_name: str | None, path: str
) -> None:
    """Insert a chunk's row into `chunks_fts` (replacing any existing row
    for this chunk id first — see delete_chunk)."""
    delete_chunk(conn, chunk_id)
    name = symbol_name or ""
    searchable_name = f"{name} {split_identifier(name)}".strip() if name else ""
    conn.execute(
        "INSERT INTO chunks_fts (rowid, content, symbol_name, path) VALUES (?, ?, ?, ?)",
        (chunk_id, content, searchable_name, path),
    )


def delete_chunk(conn: sqlite3.Connection, chunk_id: int) -> None:
    conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))


def _fts_match_expr(query: str) -> str:
    """FTS5's MATCH syntax treats punctuation specially (AND/OR/NOT,
    parentheses, quotes, ...) — quoting each token individually and OR-ing
    them together treats an arbitrary user question as a plain bag of
    words instead of risking a query-syntax error on it."""
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


def bm25_search(conn: sqlite3.Connection, query: str, k: int) -> list[int]:
    """Best-first chunk ids by BM25 rank. Column weights (content,
    symbol_name, path — declaration order): identifier matches are a
    stronger signal than body-text matches, path matches weaker still."""
    if k <= 0:
        return []
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
        "ORDER BY bm25(chunks_fts, 1.0, 2.0, 0.5) LIMIT ?",
        (_fts_match_expr(query), k),
    ).fetchall()
    return [r["rowid"] for r in rows]
