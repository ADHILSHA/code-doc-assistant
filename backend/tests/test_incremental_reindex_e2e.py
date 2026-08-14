"""Incremental reindex, end to end through the real `run_index_job`
pipeline (SPEC.md §6 Phase 4 task 4 and its acceptance criteria) — a copy
of mini_repo so a file can genuinely be deleted from it between two index
runs, the way a real repo commit would.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from app import jobs
from app.db import get_registry_connection, get_repo_connection
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import MINI_REPO, make_settings


def _index_a_copy(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_copy = tmp_path / "repo_copy"
    shutil.copytree(MINI_REPO, repo_copy)
    repo_id, job_id = jobs.create_repo_and_job(str(repo_copy), settings)
    jobs.run_index_job(job_id, repo_id, str(repo_copy), settings)
    return settings, repo_copy, repo_id


def test_reindex_after_deleting_a_file_removes_its_rows_and_only_its_rows(tmp_path: Path):
    settings, repo_copy, repo_id = _index_a_copy(tmp_path)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)

    other_file_id = conn.execute(
        "SELECT id FROM files WHERE path = 'src/auth/auth.py'"
    ).fetchone()["id"]
    other_chunk_count_before = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE file_id = ?", (other_file_id,)
    ).fetchone()["n"]

    (repo_copy / "src" / "auth" / "crypto.py").unlink()
    jobs.run_index_job(uuid.uuid4().hex, repo_id, str(repo_copy), settings)

    assert conn.execute("SELECT 1 FROM files WHERE path = 'src/auth/crypto.py'").fetchone() is None
    # An unrelated file's own rows are untouched by the removal.
    other_chunk_count_after = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE file_id = ?", (other_file_id,)
    ).fetchone()["n"]
    assert other_chunk_count_after == other_chunk_count_before

    registry_conn = get_registry_connection(settings)
    stats = json.loads(
        registry_conn.execute("SELECT stats_json FROM repos WHERE id = ?", (repo_id,)).fetchone()[0]
    )
    assert stats["files_removed"] == 1
    assert stats["files_unchanged"] > 0  # everything else was recognized as unchanged, not re-processed


def test_reindex_after_a_one_file_change_is_fast(tmp_path: Path):
    """SPEC.md §6 Phase 4 acceptance criterion: "Reindexing after a 1-file
    change touches only that file's rows and takes <5 seconds." Local
    sources never re-clone (there's nothing to fetch — jobs.py walks the
    working tree in place), so this is really exercising index_file's
    existing content_hash short-circuit at repo scale, now proven with a
    real timing assertion rather than just "it must be fast, trust me."""
    settings, repo_copy, repo_id = _index_a_copy(tmp_path)

    (repo_copy / "src" / "auth" / "auth.py").write_text(
        (repo_copy / "src" / "auth" / "auth.py").read_text() + "\n# a trivial one-line change\n"
    )

    start = time.monotonic()
    jobs.run_index_job(uuid.uuid4().hex, repo_id, str(repo_copy), settings)
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"reindex after a 1-file change took {elapsed:.2f}s"

    registry_conn = get_registry_connection(settings)
    stats = json.loads(
        registry_conn.execute("SELECT stats_json FROM repos WHERE id = ?", (repo_id,)).fetchone()[0]
    )
    assert stats["files_changed"] == 1
    assert stats["files_added"] == 0
    assert stats["files_removed"] == 0
