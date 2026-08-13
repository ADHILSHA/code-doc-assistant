"""DB writes for symbols/refs/import-edges/dependencies/endpoints (SPEC.md
§6 Phase 2), wired into jobs.py's indexing loop alongside index/store.py's
chunk indexing.

Two-phase, for the same reason chunking can't see other files while
walking: call/import resolution needs the *whole* repo's symbol table and
file-path set, not just the current file's.

  - `index_file_graph` (phase 1, called once per file inside jobs.py's main
    loop) writes `symbols`, `endpoints`, and — for manifest files —
    `dependencies`, none of which need cross-file information, and returns
    a `PendingFile` staging that file's raw calls/imports for phase 2.
  - `resolve_and_write_refs` (phase 2, called once after the loop) resolves
    every staged call against the now-complete `symbols` table and every
    staged import against the now-complete `files` table, and writes
    `symbol_refs`/`import_edges`.

Each file's rows are deleted-and-reinserted on (re)index, matching the
`chunks` table's pattern in index/store.py — safe to call on every
(re)indexed file, not just new ones.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.parsing.extractors.dependencies import (
    Dependency,
    extract_dependencies,
    is_manifest_filename,
)
from app.parsing.extractors.endpoints.common import Endpoint
from app.parsing.extractors.endpoints.dispatch import extract_endpoints
from app.parsing.refs import CallRef, ImportRef, extract_calls, extract_imports, resolve_import
from app.parsing.symbols import Symbol, extract_symbols


@dataclass(frozen=True)
class PendingFile:
    file_id: int
    path: str
    language: str | None
    calls: list[CallRef]
    imports: list[ImportRef]
    symbol_ranges: list[tuple[int, int, int]]  # (start_line, end_line, symbol_id) — for enclosing-symbol lookup


def index_file_graph(
    conn: sqlite3.Connection, file_id: int, path: str, text: str, language: str | None
) -> PendingFile:
    symbol_ranges = _write_symbols(conn, file_id, extract_symbols(text, language))
    _write_endpoints(conn, file_id, extract_endpoints(text, path, language))
    if is_manifest_filename(PurePosixPath(path).name):
        _write_dependencies(conn, path, extract_dependencies(path, text))
    return PendingFile(
        file_id=file_id,
        path=path,
        language=language,
        calls=extract_calls(text, language),
        imports=extract_imports(text, language),
        symbol_ranges=symbol_ranges,
    )


def _write_symbols(conn: sqlite3.Connection, file_id: int, symbols: list[Symbol]) -> list[tuple[int, int, int]]:
    conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
    ranges: list[tuple[int, int, int]] = []
    for s in symbols:
        cur = conn.execute(
            "INSERT INTO symbols "
            "(file_id, name, kind, signature, docstring, parent_symbol, start_line, end_line, is_exported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id, s.name, s.kind, s.signature, s.docstring, s.parent_symbol,
                s.start_line, s.end_line, int(s.is_exported),
            ),
        )
        assert cur.lastrowid is not None
        ranges.append((s.start_line, s.end_line, cur.lastrowid))
    return ranges


def _write_endpoints(conn: sqlite3.Connection, file_id: int, endpoints: list[Endpoint]) -> None:
    conn.execute("DELETE FROM endpoints WHERE file_id = ?", (file_id,))
    for e in endpoints:
        conn.execute(
            "INSERT INTO endpoints "
            "(method, route, framework, handler_symbol, file_id, line, auth_hint, params_json, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e.method, e.route, e.framework, e.handler_symbol, file_id, e.line, e.auth_hint, e.params_json, e.source),
        )


def _write_dependencies(conn: sqlite3.Connection, manifest_path: str, deps: list[Dependency]) -> None:
    # Keyed by manifest_path, not file_id — dependencies has no file_id
    # column (SPEC.md §4: a manifest describes the repo's dependency set,
    # not a single file's), so re-parsing the same manifest on reindex
    # replaces its rows by path instead.
    conn.execute("DELETE FROM dependencies WHERE manifest_path = ?", (manifest_path,))
    for d in deps:
        conn.execute(
            "INSERT INTO dependencies (ecosystem, name, version_spec, kind, manifest_path, used_in_files_json) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (d.ecosystem, d.name, d.version_spec, d.kind, manifest_path),
        )


def resolve_and_write_refs(conn: sqlite3.Connection, pending: list[PendingFile]) -> None:
    """`pending` only needs to cover the files that were (re)indexed this
    run — their `symbol_refs`/`import_edges` rows get deleted-and-reinserted
    here. Unchanged files keep whatever refs/edges they already have from a
    previous run. But `known_paths`/`path_to_file_id` (used to resolve an
    import to a target file) and `name_index` (used to resolve a call to a
    target symbol) are read fresh from `files`/`symbols` — covering every
    file in the repo, not just `pending` — so a changed file that imports
    from (or calls into) an *unchanged* file still resolves correctly."""
    known_paths = {row["path"] for row in conn.execute("SELECT path FROM files")}
    path_to_file_id = {row["path"]: row["id"] for row in conn.execute("SELECT id, path FROM files")}

    name_index: dict[str, list[tuple[int, int]]] = {}
    for row in conn.execute("SELECT id, name, file_id FROM symbols"):
        name_index.setdefault(row["name"], []).append((row["file_id"], row["id"]))

    for p in pending:
        conn.execute("DELETE FROM symbol_refs WHERE from_file_id = ?", (p.file_id,))
        conn.execute("DELETE FROM import_edges WHERE from_file_id = ?", (p.file_id,))

        for call in p.calls:
            from_symbol_id = _enclosing_symbol(call.line, p.symbol_ranges)
            resolved_symbol_id = _resolve_call(call.target_name, p.file_id, name_index)
            conn.execute(
                "INSERT INTO symbol_refs (from_file_id, from_symbol_id, target_name, resolved_symbol_id, line) "
                "VALUES (?, ?, ?, ?, ?)",
                (p.file_id, from_symbol_id, call.target_name, resolved_symbol_id, call.line),
            )

        for imp in p.imports:
            resolved_path, is_external = resolve_import(
                imp.module_text, from_path=p.path, language=p.language, known_paths=known_paths
            )
            to_file_id = path_to_file_id.get(resolved_path) if resolved_path else None
            conn.execute(
                "INSERT INTO import_edges (from_file_id, to_file_id, module_text, is_external, line) "
                "VALUES (?, ?, ?, ?, ?)",
                (p.file_id, to_file_id, imp.module_text, int(is_external), imp.line),
            )
    conn.commit()


def _enclosing_symbol(line: int, ranges: list[tuple[int, int, int]]) -> int | None:
    """Smallest range containing `line` — same "innermost wins" rule as a
    nested function inside a class: both contain the line, the narrower one
    is the more useful attribution."""
    best_span: int | None = None
    best_id: int | None = None
    for start, end, symbol_id in ranges:
        if start <= line <= end:
            span = end - start
            if best_span is None or span < best_span:
                best_span, best_id = span, symbol_id
    return best_id


def _resolve_call(name: str, from_file_id: int, name_index: dict[str, list[tuple[int, int]]]) -> int | None:
    """None (left unresolved) whenever more than one symbol could plausibly
    be the target — a wrong guess would make `symbol_refs` actively
    misleading, whereas NULL is honestly "we don't know"."""
    candidates = name_index.get(name)
    if not candidates:
        return None
    same_file = [sid for fid, sid in candidates if fid == from_file_id]
    if len(same_file) == 1:
        return same_file[0]
    if same_file:
        return None  # multiple same-named symbols in this file — ambiguous
    if len(candidates) == 1:
        return candidates[0][1]
    return None  # multiple same-named symbols across the repo — ambiguous
