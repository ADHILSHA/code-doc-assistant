"""Incremental reindex (SPEC.md §6 Phase 4 task 4): diff a fresh walk
against the `files` table, and clean up every dependent row for a file
that's been *removed* from the repo since the last index.

`index/store.py::index_file` already skips re-chunking an unchanged file
(by `content_hash`) — that half of "incremental" was true since Phase 0.
What was missing: nothing ever deleted a removed file's rows. A file that
disappears from the repo (renamed, deleted, moved) would leave its
`chunks`/`symbols`/`symbol_refs`/`import_edges`/`endpoints`/`chunk_vectors`/
lexical-index rows behind forever — stale content silently answering future
queries. `diff_files` finds those files; `remove_stale_files` deletes them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.index import lexical
from app.ingest.walker import DiscoveredFile


@dataclass(frozen=True)
class FileDiff:
    added: list[str]
    changed: list[str]
    unchanged: list[str]
    removed: list[str]


def diff_files(conn: sqlite3.Connection, discovered: list[DiscoveredFile]) -> FileDiff:
    """Read-only — compares `discovered` (this run's fresh walk) against
    `files` as it stood *before* this run touches it, so call this once,
    before `index_file` starts mutating `files`."""
    existing = {r["path"]: r["content_hash"] for r in conn.execute("SELECT path, content_hash FROM files")}
    walked_paths = {d.path for d in discovered}

    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    for d in discovered:
        if d.path not in existing:
            added.append(d.path)
        elif existing[d.path] != d.content_hash:
            changed.append(d.path)
        else:
            unchanged.append(d.path)

    removed = [path for path in existing if path not in walked_paths]
    return FileDiff(added=added, changed=changed, unchanged=unchanged, removed=removed)


def remove_stale_files(conn: sqlite3.Connection, removed_paths: list[str]) -> int:
    """Deletes every row (across every table Phase 1-3 populate) for files
    no longer present in the repo. Returns how many files were removed.

    Rows a removed file's own `file_id` owns outright (chunks, symbols,
    endpoints, its own outgoing symbol_refs/import_edges) are deleted.
    Rows *elsewhere* that merely reference the removed file (a caller's
    `symbol_refs.resolved_symbol_id`, an importer's `import_edges.to_file_id`)
    are nulled out instead of deleted — the referencing row's own file is
    still real, only its resolution target is gone, same spirit as a
    citation that no longer resolves rather than a fabricated one.
    """
    count = 0
    for path in removed_paths:
        row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            continue
        file_id = row["id"]

        chunk_ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))]
        if chunk_ids:
            placeholders = ",".join("?" for _ in chunk_ids)
            conn.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({placeholders})", chunk_ids)
            for chunk_id in chunk_ids:
                lexical.delete_chunk(conn, chunk_id)
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))

        # Null out dangling cross-references *before* deleting the symbols
        # they point at — the subquery needs those rows to still exist.
        conn.execute(
            "UPDATE symbol_refs SET resolved_symbol_id = NULL WHERE resolved_symbol_id IN "
            "(SELECT id FROM symbols WHERE file_id = ?)",
            (file_id,),
        )
        conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM symbol_refs WHERE from_file_id = ?", (file_id,))

        conn.execute("UPDATE import_edges SET to_file_id = NULL WHERE to_file_id = ?", (file_id,))
        conn.execute("DELETE FROM import_edges WHERE from_file_id = ?", (file_id,))

        conn.execute("DELETE FROM endpoints WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM dependencies WHERE manifest_path = ?", (path,))
        conn.execute("DELETE FROM summaries WHERE scope = 'file' AND target_path = ?", (path,))
        # Directory/repo-level summaries aren't deleted here — their own
        # `source_hash` is derived from their children's summaries
        # (enrich/summarizer.py), so they'll naturally be recognized as
        # stale and regenerated next time summarize_repo runs, without
        # needing this module to know their scope-specific hashing rules.

        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        count += 1

    conn.commit()
    return count
