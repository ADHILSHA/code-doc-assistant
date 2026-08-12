"""Phase 0 naive chunker + writes to `files`/`chunks`.

Fixed ~1500-char windows with 200-char overlap (SPEC.md §6 Phase 0 task 4).
Replaced by tree-sitter AST-aware chunking in Phase 1 (`parsing/chunker.py`);
kept intentionally simple here since it's throwaway.

Idempotency: `index_file` keys off `files.path` + `content_hash`. Re-running
on an unchanged file is a no-op (existing chunk rows are reused, not
duplicated); a changed file has its old chunks deleted and replaced.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.ingest.walker import DiscoveredFile


@dataclass(frozen=True)
class FileIndexResult:
    file_id: int
    chunk_ids: list[int]
    changed: bool  # False => file was already indexed with identical content, nothing rewritten


def naive_chunk_text(
    text: str, *, size: int = 1500, overlap: int = 200
) -> list[tuple[int, int, str]]:
    """Split `text` into fixed-size character windows with overlap. Returns
    (start_line, end_line, content) tuples, 1-indexed inclusive line numbers.
    """
    if not text:
        return []
    n = len(text)

    def line_at(idx: int) -> int:
        """1-indexed line number containing character index `idx` (0 <= idx < n)."""
        return text.count("\n", 0, idx) + 1

    if n <= size:
        return [(1, line_at(n - 1), text)]

    step = max(size - overlap, 1)
    windows: list[tuple[int, int, str]] = []
    pos = 0
    while pos < n:
        end = min(pos + size, n)
        # `end - 1` (not `end`): `end` is an exclusive slice bound, so the
        # line number we want is that of the last character actually
        # included in the window.
        windows.append((line_at(pos), line_at(end - 1), text[pos:end]))
        if end == n:
            break
        pos += step
    return windows


def index_file(conn: sqlite3.Connection, discovered: DiscoveredFile) -> FileIndexResult:
    row = conn.execute(
        "SELECT id, content_hash FROM files WHERE path = ?", (discovered.path,)
    ).fetchone()

    if row is not None and row["content_hash"] == discovered.content_hash:
        unchanged_chunk_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM chunks WHERE file_id = ? ORDER BY id", (row["id"],)
            )
        ]
        return FileIndexResult(file_id=row["id"], chunk_ids=unchanged_chunk_ids, changed=False)

    if row is not None:
        file_id = row["id"]
        conn.execute(
            "UPDATE files SET language=?, content_hash=?, size_bytes=?, loc=?, is_test=? "
            "WHERE id=?",
            (
                discovered.language,
                discovered.content_hash,
                discovered.size_bytes,
                discovered.loc,
                int(discovered.is_test),
                file_id,
            ),
        )
        # Drop stale vectors for the chunks we're about to replace too, so a
        # content change doesn't leave orphaned rows in chunk_vectors.
        # (Cleanup for files removed from the repo entirely — vs. changed —
        # is Phase 4's incremental-reindex job.)
        old_chunk_ids = [
            r["id"] for r in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))
        ]
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        if old_chunk_ids:
            placeholders = ",".join("?" for _ in old_chunk_ids)
            conn.execute(
                f"DELETE FROM chunk_vectors WHERE chunk_id IN ({placeholders})", old_chunk_ids
            )
    else:
        cur = conn.execute(
            "INSERT INTO files (path, language, content_hash, size_bytes, loc, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                discovered.path,
                discovered.language,
                discovered.content_hash,
                discovered.size_bytes,
                discovered.loc,
                int(discovered.is_test),
            ),
        )
        file_id = cur.lastrowid
        assert file_id is not None

    header = f"# {discovered.path}"
    chunk_ids: list[int] = []
    for start_line, end_line, content in naive_chunk_text(discovered.text):
        cur = conn.execute(
            "INSERT INTO chunks "
            "(file_id, symbol_name, symbol_kind, parent_symbol, start_line, end_line, "
            " header, content, token_count) "
            "VALUES (?, NULL, 'module', NULL, ?, ?, ?, ?, ?)",
            (file_id, start_line, end_line, header, content, max(1, len(content) // 4)),
        )
        assert cur.lastrowid is not None
        chunk_ids.append(cur.lastrowid)

    return FileIndexResult(file_id=file_id, chunk_ids=chunk_ids, changed=True)
