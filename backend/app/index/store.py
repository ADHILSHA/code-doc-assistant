"""Chunking (via parsing/chunker.py) + writes to `files`/`chunks`/`chunks_fts`.

Idempotency: `index_file` keys off `files.path` + `content_hash`. Re-running
on an unchanged file is a no-op (existing chunk rows are reused, not
duplicated); a changed file has its old chunks — and their lexical-index
and vector rows — deleted and replaced.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.index import lexical
from app.ingest.redact import redact_secrets
from app.ingest.walker import DiscoveredFile
from app.parsing.chunker import chunk_file
from app.parsing.imports import extract_imports


@dataclass(frozen=True)
class FileIndexResult:
    file_id: int
    chunk_ids: list[int]
    changed: bool  # False => file was already indexed with identical content, nothing rewritten
    # "ast" | "markdown" | "naive" for a (re)chunked file; None if `changed`
    # is False (nothing was rechunked, so there's no strategy to report).
    chunking_strategy: str | None


def _build_header(path: str, parent_symbol: str | None, imports: list[str]) -> str:
    """SPEC.md §6 Phase 1 task 1: "# path/to/file.py | class UserService |
    imports: fastapi, sqlalchemy" — injected context prepended before
    embedding, stored separately from `content` so it's never shown to the
    user as code."""
    parts = [f"# {path}"]
    if parent_symbol:
        parts.append(f"class {parent_symbol}")
    if imports:
        parts.append(f"imports: {', '.join(imports)}")
    return " | ".join(parts)


def index_file(
    conn: sqlite3.Connection,
    discovered: DiscoveredFile,
    *,
    max_tokens: int = 800,
    overlap_statements: int = 1,
    naive_size: int = 1500,
    naive_overlap: int = 200,
) -> FileIndexResult:
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
        return FileIndexResult(
            file_id=row["id"], chunk_ids=unchanged_chunk_ids, changed=False, chunking_strategy=None
        )

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
        # Drop stale vectors and lexical-index rows for the chunks we're
        # about to replace too, so a content change doesn't leave orphans.
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
            for old_id in old_chunk_ids:
                lexical.delete_chunk(conn, old_id)
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

    result = chunk_file(
        discovered.text,
        discovered.language,
        max_tokens=max_tokens,
        overlap_statements=overlap_statements,
        naive_size=naive_size,
        naive_overlap=naive_overlap,
    )
    imports = extract_imports(discovered.text, discovered.language)

    chunk_ids: list[int] = []
    for c in result.chunks:
        header = _build_header(discovered.path, c.parent_symbol, imports)
        # SPEC.md §6 Phase 5 task 3 / §7.5: redact before storage — this is
        # the single choke point every downstream consumer of chunk content
        # goes through (embedding, the synthesis/agent prompt context,
        # semantic_search results), so redacting here covers all of them at
        # once rather than needing a redaction call at each call site.
        content = redact_secrets(c.content)
        cur = conn.execute(
            "INSERT INTO chunks "
            "(file_id, symbol_name, symbol_kind, parent_symbol, start_line, end_line, "
            " header, content, token_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                c.symbol_name,
                c.symbol_kind,
                c.parent_symbol,
                c.start_line,
                c.end_line,
                header,
                content,
                max(1, len(content) // 4),
            ),
        )
        assert cur.lastrowid is not None
        chunk_id = cur.lastrowid
        chunk_ids.append(chunk_id)
        lexical.index_chunk(
            conn, chunk_id, content=content, symbol_name=c.symbol_name, path=discovered.path
        )

    return FileIndexResult(
        file_id=file_id, chunk_ids=chunk_ids, changed=True, chunking_strategy=result.strategy
    )
