"""Connection factory + migrations for both the global registry DB and
per-repo DBs (SPEC.md §4). Migrations are plain ordered SQL scripts tracked
via `PRAGMA user_version` — each phase appends to its migration list rather
than mutating history, since there's no production data to preserve across
phases of this build.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from app.config import Settings, get_settings

# --- registry db (data/dbs/_registry.db) ---

REGISTRY_MIGRATIONS: list[str] = [
    # v1 (Phase 0)
    """
    CREATE TABLE repos (
      id TEXT PRIMARY KEY,
      source TEXT NOT NULL,
      source_type TEXT NOT NULL,
      display_name TEXT NOT NULL,
      local_path TEXT NOT NULL,
      commit_sha TEXT,
      default_branch TEXT,
      status TEXT NOT NULL,
      error TEXT,
      stats_json TEXT,
      created_at TEXT NOT NULL,
      indexed_at TEXT
    );

    CREATE TABLE jobs (
      id TEXT PRIMARY KEY,
      repo_id TEXT NOT NULL,
      type TEXT NOT NULL,
      state TEXT NOT NULL,
      stage TEXT,
      progress REAL NOT NULL DEFAULT 0,
      message TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_jobs_repo ON jobs(repo_id);
    """,
    # v2 (Phase 5 task 2): encrypted-at-rest credentials (a GitHub PAT used
    # as a git credential for private-repo clone/fetch — see
    # app/security/credentials.py). `provider` is a fixed key ("github" for
    # now) rather than a free-form label, so there's at most one stored
    # token per provider; `token_encrypted` is the Fernet ciphertext, never
    # plaintext.
    """
    CREATE TABLE credentials (
      id INTEGER PRIMARY KEY,
      provider TEXT UNIQUE NOT NULL,
      token_encrypted BLOB NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    """,
]

# --- per-repo db (data/dbs/{repo_id}.db) ---

REPO_MIGRATIONS: list[str] = [
    # v1 (Phase 0) — files + naive chunks + query log.
    # symbols/refs/dependencies/endpoints/summaries/chunks_fts are added by
    # later phases' migrations as those layers are built.
    """
    CREATE TABLE files (
      id INTEGER PRIMARY KEY,
      path TEXT UNIQUE NOT NULL,
      language TEXT,
      content_hash TEXT NOT NULL,
      size_bytes INTEGER,
      loc INTEGER,
      is_test INTEGER DEFAULT 0,
      last_commit_sha TEXT
    );

    CREATE TABLE chunks (
      -- AUTOINCREMENT (deviates from SPEC.md §4, which doesn't specify it):
      -- chunk ids get deleted and reinserted whenever a file's content
      -- changes (index/store.py::index_file), and a plain `INTEGER PRIMARY
      -- KEY` reuses deleted rowids. Without AUTOINCREMENT, a reused id could
      -- make an old `query_log.chunk_ids_json` entry silently point at
      -- unrelated content after a later reindex. See DECISIONS.md.
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      file_id INTEGER NOT NULL REFERENCES files(id),
      symbol_name TEXT,
      symbol_kind TEXT,
      parent_symbol TEXT,
      start_line INTEGER NOT NULL,
      end_line INTEGER NOT NULL,
      header TEXT,
      content TEXT NOT NULL,
      token_count INTEGER
    );
    CREATE INDEX idx_chunks_file ON chunks(file_id);

    CREATE TABLE query_log (
      id INTEGER PRIMARY KEY,
      question TEXT,
      route TEXT,
      answer TEXT,
      citations_json TEXT,
      chunk_ids_json TEXT,
      tool_calls INTEGER,
      latency_ms INTEGER,
      input_tokens INTEGER,
      output_tokens INTEGER,
      created_at TEXT
    );
    """,
    # v2 (Phase 1) — lexical index (index/lexical.py), keyed by chunk id as
    # rowid. NOT contentless (deviates from SPEC.md §4's `content=''`) — see
    # DECISIONS.md: contentless FTS5 tables can't be deleted from by rowid
    # alone (confirmed by testing, not by inspection), only via a special
    # command re-supplying the exact original indexed values, which would
    # push real coupling into index/store.py's chunk-replacement path for a
    # storage micro-optimization that doesn't matter at this scale.
    """
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
      content, symbol_name, path,
      tokenize='unicode61 remove_diacritics 0'
    );
    """,
    # v3 (Phase 2) — symbols, call/import graph, dependencies, endpoints.
    """
    CREATE TABLE symbols (
      -- AUTOINCREMENT for the same reason as chunks.id (see v1 above):
      -- symbol_refs.resolved_symbol_id points at these ids, and
      -- parsing/symbols.py deletes-and-reinserts a file's symbols on
      -- reindex.
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      file_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      kind TEXT NOT NULL,
      signature TEXT,
      docstring TEXT,
      parent_symbol TEXT,
      start_line INTEGER,
      end_line INTEGER,
      is_exported INTEGER DEFAULT 1
    );
    CREATE INDEX idx_symbols_name ON symbols(name);
    CREATE INDEX idx_symbols_file ON symbols(file_id);

    CREATE TABLE symbol_refs (
      id INTEGER PRIMARY KEY,
      from_file_id INTEGER,
      from_symbol_id INTEGER,
      target_name TEXT NOT NULL,
      resolved_symbol_id INTEGER,
      line INTEGER
    );
    CREATE INDEX idx_refs_target ON symbol_refs(target_name);
    CREATE INDEX idx_refs_file ON symbol_refs(from_file_id);

    CREATE TABLE import_edges (
      id INTEGER PRIMARY KEY,
      from_file_id INTEGER NOT NULL,
      to_file_id INTEGER,
      module_text TEXT NOT NULL,
      is_external INTEGER,
      line INTEGER
    );
    CREATE INDEX idx_import_edges_file ON import_edges(from_file_id);

    CREATE TABLE dependencies (
      id INTEGER PRIMARY KEY,
      ecosystem TEXT,
      name TEXT NOT NULL,
      version_spec TEXT,
      kind TEXT,
      manifest_path TEXT NOT NULL,
      used_in_files_json TEXT
    );

    CREATE TABLE endpoints (
      id INTEGER PRIMARY KEY,
      method TEXT,
      route TEXT NOT NULL,
      framework TEXT,
      handler_symbol TEXT,
      file_id INTEGER,
      line INTEGER,
      auth_hint TEXT,
      params_json TEXT,
      source TEXT
    );
    """,
    # v4 (Phase 3) — hierarchical summaries + session memory.
    #
    # `summaries` is verbatim from SPEC.md §4. `query_log.session_id` is
    # NOT in §4's schema — added because "rewrite follow-up questions
    # against the last 3 turns" (SPEC.md §6 Phase 3 task 5) needs a way to
    # find "the last 3 turns of *this conversation*", and query_log is
    # already the per-repo turn history; a bare ALTER TABLE ADD COLUMN
    # keeps that history intact instead of introducing a parallel
    # conversation-log table. See DECISIONS.md.
    """
    CREATE TABLE summaries (
      id INTEGER PRIMARY KEY,
      scope TEXT,
      target_path TEXT NOT NULL,
      content TEXT NOT NULL,
      source_hash TEXT NOT NULL,
      model TEXT,
      created_at TEXT
    );
    CREATE INDEX idx_summaries_scope_path ON summaries(scope, target_path);

    ALTER TABLE query_log ADD COLUMN session_id TEXT;
    CREATE INDEX idx_query_log_session ON query_log(session_id);
    """,
]


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _apply_migrations(conn: sqlite3.Connection, migrations: list[str]) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(current, len(migrations)):
        conn.executescript(migrations[i])
        conn.execute(f"PRAGMA user_version = {i + 1}")
    conn.commit()


def get_registry_connection(settings: Settings | None = None) -> sqlite3.Connection:
    settings = settings or get_settings()
    conn = _connect(settings.registry_db_path)
    _apply_migrations(conn, REGISTRY_MIGRATIONS)
    return conn


def get_repo_connection(
    repo_id: str, embedding_dim: int, settings: Settings | None = None
) -> sqlite3.Connection:
    """Open (creating + migrating if needed) the per-repo DB, with sqlite-vec
    loaded and `chunk_vectors` sized for `embedding_dim`.

    `embedding_dim` must match whichever EmbeddingProvider indexed this repo
    (voyage-code-3=1024, OpenAI's text-embedding-3-small=1536, ...) — see
    `_ensure_vector_table`.
    """
    settings = settings or get_settings()
    conn = _connect(settings.repo_db_path(repo_id))

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    _apply_migrations(conn, REPO_MIGRATIONS)
    _ensure_vector_table(conn, embedding_dim)
    return conn


def _ensure_vector_table(conn: sqlite3.Connection, dim: int) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vectors'"
    ).fetchone()
    if row is not None:
        if f"FLOAT[{dim}]" not in row["sql"]:
            raise RuntimeError(
                "chunk_vectors was created with a different embedding dimension "
                f"than the current provider ({dim}). Delete the repo DB and reindex "
                "(incremental reindex with a provider change isn't supported)."
            )
        return
    conn.execute(
        "CREATE VIRTUAL TABLE chunk_vectors USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
    )
    conn.commit()
