"""Incremental reindex diff + orphaned-row cleanup (SPEC.md §6 Phase 4
task 4), against the real indexed mini_repo fixture.
"""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.db import get_repo_connection
from app.ingest.incremental import diff_files, remove_stale_files
from app.ingest.walker import walk_repo
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import MINI_REPO, make_settings


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    return get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)


def test_diff_files_reports_no_changes_on_an_identical_walk(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    kept, _skipped = walk_repo(MINI_REPO)

    diff = diff_files(conn, kept)
    assert diff.added == []
    assert diff.changed == []
    assert diff.removed == []
    assert len(diff.unchanged) == len(kept)


def test_diff_files_detects_a_removed_file(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    kept, _skipped = walk_repo(MINI_REPO)
    filtered = [d for d in kept if d.path != "src/auth/crypto.py"]

    diff = diff_files(conn, filtered)
    assert diff.removed == ["src/auth/crypto.py"]
    assert diff.added == []
    assert diff.changed == []


def test_diff_files_detects_a_changed_file(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    kept, _skipped = walk_repo(MINI_REPO)

    import dataclasses

    mutated = [
        dataclasses.replace(d, content_hash="deliberately-different") if d.path == "src/auth/auth.py" else d
        for d in kept
    ]
    diff = diff_files(conn, mutated)
    assert diff.changed == ["src/auth/auth.py"]
    assert diff.removed == []


def test_diff_files_detects_an_added_file(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    kept, _skipped = walk_repo(MINI_REPO)

    from app.ingest.walker import DiscoveredFile

    new_file = DiscoveredFile(
        path="src/brand_new.py", language="python", content_hash="new-hash",
        size_bytes=10, loc=1, is_test=False, text="x = 1\n",
    )
    diff = diff_files(conn, [*kept, new_file])
    assert diff.added == ["src/brand_new.py"]


def test_remove_stale_files_deletes_all_dependent_rows(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    file_row = conn.execute("SELECT id FROM files WHERE path = 'src/auth/crypto.py'").fetchone()
    file_id = file_row["id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE file_id = ?", (file_id,)).fetchone()["n"] > 0
    assert conn.execute("SELECT COUNT(*) AS n FROM symbols WHERE file_id = ?", (file_id,)).fetchone()["n"] > 0

    n = remove_stale_files(conn, ["src/auth/crypto.py"])
    assert n == 1

    assert conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE file_id = ?", (file_id,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM symbols WHERE file_id = ?", (file_id,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM chunk_vectors WHERE chunk_id IN "
                         "(SELECT id FROM chunks WHERE file_id = ?)", (file_id,)).fetchone()["n"] == 0


def test_remove_stale_files_nulls_dangling_cross_file_references_instead_of_deleting_them(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    # auth.py calls derive_key(), defined in crypto.py — removing crypto.py
    # must leave auth.py's own call-site row intact (it's still a real call
    # in real code) but with its resolution target cleared.
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM symbol_refs WHERE target_name = 'derive_key'"
    ).fetchone()["n"]
    assert before > 0

    remove_stale_files(conn, ["src/auth/crypto.py"])

    after = conn.execute(
        "SELECT COUNT(*) AS n FROM symbol_refs WHERE target_name = 'derive_key'"
    ).fetchone()["n"]
    still_resolved = conn.execute(
        "SELECT COUNT(*) AS n FROM symbol_refs WHERE target_name = 'derive_key' AND resolved_symbol_id IS NOT NULL"
    ).fetchone()["n"]
    assert after == before  # the referencing rows themselves weren't deleted
    assert still_resolved == 0  # but they no longer resolve to the removed file's symbol


def test_remove_stale_files_removes_dependency_rows_for_a_removed_manifest(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM dependencies WHERE manifest_path = 'package.json'"
    ).fetchone()["n"]
    assert before > 0

    remove_stale_files(conn, ["package.json"])

    after = conn.execute(
        "SELECT COUNT(*) AS n FROM dependencies WHERE manifest_path = 'package.json'"
    ).fetchone()["n"]
    assert after == 0


def test_remove_stale_files_unknown_path_is_a_no_op(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    n = remove_stale_files(conn, ["does/not/exist.py"])
    assert n == 0


def test_remove_stale_files_empty_list_is_a_no_op(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    n = remove_stale_files(conn, [])
    assert n == 0
