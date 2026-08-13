"""Hierarchical summaries (SPEC.md §6 Phase 3 task 4): file -> directory ->
repo, bottom-up, cheap model, cached by `source_hash` so an unchanged
file/directory isn't re-summarized on every reindex.

Scope decision (see DECISIONS.md): only FILE-scoped summaries get embedded
into `chunks`/`chunk_vectors` (as a `symbol_kind='summary'` chunk row
attached to that file's real `file_id`, so hybrid_search / the agent's
`semantic_search` tool can surface them like any other chunk).
Directory/repo-scoped summaries have no single `file_id` to attach to
(`chunks.file_id` is `NOT NULL`) and are read straight out of `summaries`
by scope/target_path instead — the `overview` route
(generation/answer.py) and the agent's `get_summary` tool both read the
repo/directory rows directly, which is a better fit for "answer from
summaries without reading source files" than hoping semantic search
surfaces them.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.enrich.summarizer_prompts import (
    DIRECTORY_SUMMARY_SYSTEM_PROMPT,
    FILE_SUMMARY_SYSTEM_PROMPT,
    REPO_SUMMARY_SYSTEM_PROMPT,
    build_directory_summary_user_message,
    build_file_summary_user_message,
    build_repo_summary_user_message,
)
from app.index import lexical
from app.providers.llm import LLMProvider

# Cap on how much of a file's own chunk text feeds the file-summary prompt
# — long enough to give the model real signal, short enough to keep the
# per-file summarization call cheap (this runs once per file per repo).
_MAX_FILE_CONTENT_CHARS = 6000


@dataclass(frozen=True)
class SummaryStats:
    files_summarized: int
    files_skipped_too_short: int
    files_unchanged: int
    directories_summarized: int
    repo_summarized: bool
    # (chunk_id, text) for newly-written file-summary chunks — hand this to
    # index/vectors.py::embed_and_store_chunks the same way jobs.py already
    # does for regular chunks.
    pending_embeddings: list[tuple[int, str]]


def summarize_repo(
    conn: sqlite3.Connection,
    llm_provider: LLMProvider,
    *,
    min_loc: int,
    display_name: str,
    concurrency: int = 1,
    max_files: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> SummaryStats:
    """`concurrency` > 1 runs that many file-level summarization calls
    (the LLM round-trip only — never a DB write) concurrently via a thread
    pool; `on_progress(done, total)` fires after each file's summary is
    written, from the calling thread, so a caller (jobs.py) can report
    incremental job progress instead of one static "summarizing" message
    for however long the whole stage takes. `max_files` caps how many
    *uncached* files get summarized this run (see `Settings.
    summary_max_files_per_run`) — the rest are simply left for a later run,
    not blocked on.
    """
    file_rows = conn.execute("SELECT id, path, loc, content_hash FROM files ORDER BY path").fetchall()

    files_summarized = 0
    files_skipped_too_short = 0
    files_unchanged = 0
    pending_embeddings: list[tuple[int, str]] = []
    file_summary_by_path: dict[str, str] = {}

    to_summarize: list[sqlite3.Row] = []
    for row in file_rows:
        if (row["loc"] or 0) < min_loc:
            files_skipped_too_short += 1
            continue
        cached = _cached_summary(conn, "file", row["path"], row["content_hash"])
        if cached is not None:
            file_summary_by_path[row["path"]] = cached
            files_unchanged += 1
            continue
        to_summarize.append(row)

    if max_files is not None and len(to_summarize) > max_files:
        to_summarize = to_summarize[:max_files]

    # Fetch content up front (a DB read, stays on this thread) so the
    # worker threads below only ever do the LLM call itself — no worker
    # thread touches `conn`, since a single sqlite3.Connection isn't safe
    # for concurrent use from multiple threads.
    jobs = [(row, _file_content(conn, row["id"])) for row in to_summarize]
    total = len(jobs)

    def _summarize_one(job: tuple[sqlite3.Row, str]) -> tuple[sqlite3.Row, str]:
        row, content = job
        return row, _summarize_file(llm_provider, row["path"], content)

    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for row, summary in pool.map(_summarize_one, jobs):
            _store_summary(conn, "file", row["path"], summary, row["content_hash"], llm_provider.model)
            chunk_id = _write_summary_chunk(conn, row["id"], row["path"], summary)
            pending_embeddings.append((chunk_id, summary))
            file_summary_by_path[row["path"]] = summary
            files_summarized += 1
            completed += 1
            if on_progress is not None:
                on_progress(completed, total)

    # --- directory level, deepest first so a directory's own summary can
    # draw on its already-computed subdirectories' summaries ---
    directories = _all_directories(r["path"] for r in file_rows)
    dir_summary_by_path: dict[str, str] = {}
    directories_summarized = 0

    for directory in sorted(directories, key=lambda d: -d.count("/")):
        child_files = {p: s for p, s in file_summary_by_path.items() if _parent_of(p) == directory}
        child_dirs = {d: s for d, s in dir_summary_by_path.items() if _parent_of(d) == directory}
        if not child_files and not child_dirs:
            continue  # every file under here was skipped (too short) or unchanged-but-uncached-yet

        source_hash = _combined_hash([*child_files.values(), *child_dirs.values()])
        cached = _cached_summary(conn, "directory", directory, source_hash)
        if cached is not None:
            dir_summary_by_path[directory] = cached
            continue

        summary = _summarize_directory(llm_provider, directory, child_files, child_dirs)
        _store_summary(conn, "directory", directory, summary, source_hash, llm_provider.model)
        dir_summary_by_path[directory] = summary
        directories_summarized += 1

    # --- repo level ---
    top_dirs = {d: s for d, s in dir_summary_by_path.items() if _parent_of(d) == "."}
    top_files = {p: s for p, s in file_summary_by_path.items() if _parent_of(p) == "."}
    repo_summarized = False
    if top_dirs or top_files:
        source_hash = _combined_hash([*top_dirs.values(), *top_files.values()])
        if _cached_summary(conn, "repo", ".", source_hash) is None:
            summary = _summarize_repo(llm_provider, display_name, top_dirs, top_files)
            _store_summary(conn, "repo", ".", summary, source_hash, llm_provider.model)
            repo_summarized = True

    conn.commit()
    return SummaryStats(
        files_summarized=files_summarized,
        files_skipped_too_short=files_skipped_too_short,
        files_unchanged=files_unchanged,
        directories_summarized=directories_summarized,
        repo_summarized=repo_summarized,
        pending_embeddings=pending_embeddings,
    )


def _parent_of(path: str) -> str:
    """Works uniformly for both file paths and directory paths — a
    top-level file's or top-level directory's parent is always `"."`."""
    return PurePosixPath(path).parent.as_posix()


def _all_directories(paths: Iterable[str]) -> set[str]:
    dirs: set[str] = set()
    for p in paths:
        parts = PurePosixPath(p).parent.parts
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return dirs


def _combined_hash(parts: list[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(parts):
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _cached_summary(conn: sqlite3.Connection, scope: str, target_path: str, source_hash: str) -> str | None:
    row = conn.execute(
        "SELECT content FROM summaries WHERE scope = ? AND target_path = ? AND source_hash = ?",
        (scope, target_path, source_hash),
    ).fetchone()
    return row["content"] if row else None


def _store_summary(
    conn: sqlite3.Connection, scope: str, target_path: str, content: str, source_hash: str, model: str
) -> None:
    conn.execute("DELETE FROM summaries WHERE scope = ? AND target_path = ?", (scope, target_path))
    conn.execute(
        "INSERT INTO summaries (scope, target_path, content, source_hash, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (scope, target_path, content, source_hash, model),
    )


def _file_content(conn: sqlite3.Connection, file_id: int) -> str:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE file_id = ? AND (symbol_kind IS NULL OR symbol_kind != 'summary') "
        "ORDER BY start_line",
        (file_id,),
    ).fetchall()
    return "\n".join(r["content"] for r in rows)[:_MAX_FILE_CONTENT_CHARS]


def _write_summary_chunk(conn: sqlite3.Connection, file_id: int, path: str, summary: str) -> int:
    old = conn.execute(
        "SELECT id FROM chunks WHERE file_id = ? AND symbol_kind = 'summary'", (file_id,)
    ).fetchone()
    if old is not None:
        conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (old["id"],))
        lexical.delete_chunk(conn, old["id"])
        conn.execute("DELETE FROM chunks WHERE id = ?", (old["id"],))

    cur = conn.execute(
        "INSERT INTO chunks (file_id, symbol_name, symbol_kind, parent_symbol, start_line, end_line, "
        "header, content, token_count) VALUES (?, NULL, 'summary', NULL, 1, 1, ?, ?, ?)",
        (file_id, f"# {path} | summary", summary, max(1, len(summary) // 4)),
    )
    assert cur.lastrowid is not None
    lexical.index_chunk(conn, cur.lastrowid, content=summary, symbol_name=None, path=path)
    return cur.lastrowid


def _summarize_file(llm_provider: LLMProvider, path: str, content: str) -> str:
    resp = llm_provider.complete(
        system=FILE_SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_file_summary_user_message(path, content)}],
        max_tokens=300,
    )
    return resp.text.strip()


def _summarize_directory(
    llm_provider: LLMProvider, directory: str, child_files: dict[str, str], child_dirs: dict[str, str]
) -> str:
    resp = llm_provider.complete(
        system=DIRECTORY_SUMMARY_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": build_directory_summary_user_message(directory, child_files, child_dirs)}
        ],
        max_tokens=300,
    )
    return resp.text.strip()


def _summarize_repo(
    llm_provider: LLMProvider, display_name: str, top_dirs: dict[str, str], top_files: dict[str, str]
) -> str:
    resp = llm_provider.complete(
        system=REPO_SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_repo_summary_user_message(display_name, top_dirs, top_files)}],
        max_tokens=400,
    )
    return resp.text.strip()
