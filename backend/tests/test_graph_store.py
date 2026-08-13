"""Integration coverage for the Phase 2 pipeline end to end: jobs.py's
indexing loop wired to index/graph_store.py, against the real mini_repo
fixture. Exercises the same path SPEC.md's acceptance criteria describe:
symbol/endpoint/dependency counts, `get_definition`, and cross-file
`symbol_refs`/`import_edges` resolution.
"""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.db import get_repo_connection
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.structured import get_definition, list_dependencies, list_endpoints

from .conftest import MINI_REPO, make_settings


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    return conn, repo_id


def test_indexing_populates_symbols_endpoints_dependencies(tmp_path: Path):
    conn, _ = _index_mini_repo(tmp_path)

    symbols = conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
    endpoints = conn.execute("SELECT COUNT(*) AS n FROM endpoints").fetchone()["n"]
    dependencies = conn.execute("SELECT COUNT(*) AS n FROM dependencies").fetchone()["n"]

    assert symbols > 0
    assert endpoints == 9  # 3 fastapi + 3 flask + 3 express, per the fixtures' ground truth
    assert dependencies == 9  # 4 npm + 5 pypi, per test_dependencies.py's exact-count assertions


def test_get_definition_returns_correct_file_and_line(tmp_path: Path):
    conn, _ = _index_mini_repo(tmp_path)

    matches = get_definition(conn, "UserService")
    assert len(matches) == 1
    assert matches[0].file_path == "src/users/service.py"
    assert matches[0].kind == "class"
    assert matches[0].start_line > 0
    assert matches[0].end_line >= matches[0].start_line

    assert get_definition(conn, "ThisSymbolDoesNotExist") == []


def test_list_endpoints_and_list_dependencies_match_direct_queries(tmp_path: Path):
    conn, _ = _index_mini_repo(tmp_path)

    endpoints = list_endpoints(conn)
    assert len(endpoints) == 9
    assert {e.framework for e in endpoints} == {"fastapi", "flask", "express"}

    deps = list_dependencies(conn)
    assert len(deps) == 9
    assert {d.ecosystem for d in deps} == {"npm", "pypi"}


def test_cross_file_call_is_resolved_to_the_defining_symbol(tmp_path: Path):
    conn, _ = _index_mini_repo(tmp_path)

    # src/users/service.py calls hash_password(), defined in src/auth/auth.py
    # (see mini_repo's UserService.create_user) — this only resolves
    # correctly if resolve_and_write_refs ran against the *whole* repo's
    # symbol table, not just the calling file's own symbols.
    row = conn.execute(
        """
        SELECT s.name, f.path
        FROM symbol_refs sr
        JOIN files caller ON caller.id = sr.from_file_id
        JOIN symbols s ON s.id = sr.resolved_symbol_id
        JOIN files f ON f.id = s.file_id
        WHERE caller.path = 'src/users/service.py' AND sr.target_name = 'hash_password'
        """
    ).fetchone()
    assert row is not None
    assert row["path"] == "src/auth/auth.py"


def test_import_edge_resolves_to_internal_file(tmp_path: Path):
    conn, _ = _index_mini_repo(tmp_path)

    row = conn.execute(
        """
        SELECT to_f.path AS to_path
        FROM import_edges ie
        JOIN files from_f ON from_f.id = ie.from_file_id
        JOIN files to_f ON to_f.id = ie.to_file_id
        WHERE from_f.path = 'web/expressRoutes.ts' AND ie.module_text = './authMiddleware'
        """
    ).fetchone()
    assert row is not None
    assert row["to_path"] == "web/authMiddleware.ts"


def test_reindexing_unchanged_repo_does_not_duplicate_rows(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    first_symbols = conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
    first_endpoints = conn.execute("SELECT COUNT(*) AS n FROM endpoints").fetchone()["n"]
    conn.close()

    # Reindex the same repo id/source with no content changes: unchanged
    # files are skipped for graph extraction (only `result.changed` files
    # call index_file_graph in jobs.py), so counts must stay identical, not
    # double. A fresh job id is enough here — run_index_job's per-job
    # bookkeeping (_update_job) is a plain UPDATE keyed on job id, and
    # doesn't require a pre-existing `jobs` row to succeed.
    import uuid as _uuid

    jobs.run_index_job(_uuid.uuid4().hex, repo_id, str(MINI_REPO), settings)
    conn2 = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    second_symbols = conn2.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
    second_endpoints = conn2.execute("SELECT COUNT(*) AS n FROM endpoints").fetchone()["n"]

    assert second_symbols == first_symbols
    assert second_endpoints == first_endpoints
