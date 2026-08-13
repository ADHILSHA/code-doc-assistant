"""Graph expansion (SPEC.md §6 Phase 3 task 1): after seed retrieval
(hybrid_search), pull in chunks a multi-hop question needs but a purely
lexical/semantic match to the *question* wouldn't surface —

  (a) definitions of symbols referenced from a seed chunk
  (b) callers of a seed chunk's own symbol
  (c) the seed chunk's file header/imports (its first "module" chunk)

— walking outward from the seed set, capped at `max_hops` hops and a
token budget, using the `symbols`/`symbol_refs` graph Phase 2 built.

Chunks and symbols are two separate tables describing mostly-the-same
definitions (see parsing/symbols.py's module docstring for why they're
extracted separately), so every hop here has to cross between them: a seed
*chunk* -> its *symbol* row (by file_id + name, nearest by line) -> graph
neighbors (other *symbol* rows, via symbol_refs) -> back to the *chunk*
that represents each neighbor (by file_id + name again, smallest match).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class _ChunkInfo:
    chunk_id: int
    file_id: int
    symbol_name: str | None
    start_line: int
    end_line: int
    token_count: int


def expand_context(
    conn: sqlite3.Connection,
    seed_chunk_ids: list[int],
    *,
    max_hops: int,
    token_budget: int,
) -> list[int]:
    """New chunk ids only (never a seed), in discovery order, cut off once
    `token_budget` (summed `chunks.token_count` of *added* chunks) is
    exhausted. Safe to call with an empty seed list (returns [])."""
    if not seed_chunk_ids:
        return []

    seen: set[int] = set(seed_chunk_ids)
    added: list[int] = []
    added_tokens = 0
    frontier = list(seed_chunk_ids)

    for _hop in range(max_hops):
        if not frontier or added_tokens >= token_budget:
            break
        next_frontier: list[int] = []

        for chunk_id in frontier:
            info = _chunk_info(conn, chunk_id)
            if info is None:
                continue

            candidates: list[int] = []
            candidates.append(_module_chunk_id(conn, info.file_id) or -1)

            symbol_id = _symbol_id_for_chunk(conn, info)
            if symbol_id is not None:
                candidates.extend(_referenced_symbol_chunks(conn, symbol_id))
                candidates.extend(_caller_chunks(conn, symbol_id))

            for candidate_id in candidates:
                if candidate_id < 0 or candidate_id in seen:
                    continue
                seen.add(candidate_id)
                tokens = _token_count(conn, candidate_id)
                if added_tokens + tokens > token_budget and added:
                    # Always let the *first* addition through even if it
                    # alone exceeds the budget — a budget of 0 shouldn't be
                    # indistinguishable from "expansion found nothing".
                    continue
                added.append(candidate_id)
                added_tokens += tokens
                next_frontier.append(candidate_id)
                if added_tokens >= token_budget:
                    break
            if added_tokens >= token_budget:
                break

        frontier = next_frontier

    return added


def _chunk_info(conn: sqlite3.Connection, chunk_id: int) -> _ChunkInfo | None:
    row = conn.execute(
        "SELECT id, file_id, symbol_name, start_line, end_line, token_count "
        "FROM chunks WHERE id = ?",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None
    return _ChunkInfo(
        chunk_id=row["id"], file_id=row["file_id"], symbol_name=row["symbol_name"],
        start_line=row["start_line"], end_line=row["end_line"],
        token_count=row["token_count"] or 0,
    )


def _token_count(conn: sqlite3.Connection, chunk_id: int) -> int:
    row = conn.execute("SELECT token_count FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    return (row["token_count"] or 0) if row else 0


def _symbol_id_for_chunk(conn: sqlite3.Connection, info: _ChunkInfo) -> int | None:
    if not info.symbol_name:
        return None
    row = conn.execute(
        "SELECT id FROM symbols WHERE file_id = ? AND name = ? "
        "ORDER BY ABS(start_line - ?) LIMIT 1",
        (info.file_id, info.symbol_name, info.start_line),
    ).fetchone()
    return row["id"] if row else None


def _chunk_id_for_symbol(conn: sqlite3.Connection, file_id: int, name: str) -> int | None:
    # Smallest-range chunk with this name wins — prefers the actual
    # function/method body over a class "shell" chunk sharing the same name
    # (see parsing/chunker.py's chunk-granularity design, SPEC.md Phase 1).
    row = conn.execute(
        "SELECT id FROM chunks WHERE file_id = ? AND symbol_name = ? "
        "ORDER BY (end_line - start_line) ASC LIMIT 1",
        (file_id, name),
    ).fetchone()
    return row["id"] if row else None


def _chunk_id_for_line(conn: sqlite3.Connection, file_id: int, line: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM chunks WHERE file_id = ? AND start_line <= ? AND end_line >= ? "
        "ORDER BY (end_line - start_line) ASC LIMIT 1",
        (file_id, line, line),
    ).fetchone()
    return row["id"] if row else None


def _module_chunk_id(conn: sqlite3.Connection, file_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM chunks WHERE file_id = ? AND symbol_kind = 'module' "
        "ORDER BY start_line ASC LIMIT 1",
        (file_id,),
    ).fetchone()
    return row["id"] if row else None


def _referenced_symbol_chunks(conn: sqlite3.Connection, symbol_id: int) -> list[int]:
    """(a): definitions of symbols this symbol's body calls."""
    out: list[int] = []
    for row in conn.execute(
        "SELECT DISTINCT s.file_id, s.name "
        "FROM symbol_refs sr JOIN symbols s ON s.id = sr.resolved_symbol_id "
        "WHERE sr.from_symbol_id = ?",
        (symbol_id,),
    ):
        chunk_id = _chunk_id_for_symbol(conn, row["file_id"], row["name"])
        if chunk_id is not None:
            out.append(chunk_id)
    return out


def _caller_chunks(conn: sqlite3.Connection, symbol_id: int) -> list[int]:
    """(b): chunks that call this symbol."""
    out: list[int] = []
    for row in conn.execute(
        "SELECT DISTINCT from_file_id, line FROM symbol_refs WHERE resolved_symbol_id = ?",
        (symbol_id,),
    ):
        chunk_id = _chunk_id_for_line(conn, row["from_file_id"], row["line"])
        if chunk_id is not None:
            out.append(chunk_id)
    return out
