"""Graph expansion (SPEC.md §6 Phase 3 task 1), against the real indexed
mini_repo fixture — expansion only makes sense with a populated
`symbols`/`symbol_refs` graph (Phase 2), so a hand-rolled DB fixture would
just be reimplementing jobs.py's own indexing loop.
"""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.db import get_repo_connection
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.expand import expand_context

from .conftest import MINI_REPO, make_settings


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    return get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)


def _chunk_id(conn, symbol_name: str, path: str) -> int:
    row = conn.execute(
        "SELECT c.id FROM chunks c JOIN files f ON f.id = c.file_id "
        "WHERE c.symbol_name = ? AND f.path = ?",
        (symbol_name, path),
    ).fetchone()
    assert row is not None, f"no chunk for {symbol_name} in {path}"
    return row["id"]


def _chunk_labels(conn, chunk_ids: list[int]) -> set[tuple[str | None, str]]:
    labels = set()
    for cid in chunk_ids:
        row = conn.execute(
            "SELECT c.symbol_name, f.path FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id = ?",
            (cid,),
        ).fetchone()
        labels.add((row["symbol_name"], row["path"]))
    return labels


def test_expand_pulls_in_a_referenced_symbols_definition_across_files(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    seed = _chunk_id(conn, "create_user", "src/users/service.py")

    added = expand_context(conn, [seed], max_hops=2, token_budget=4000)
    labels = _chunk_labels(conn, added)

    # create_user() calls hash_password(), defined in a different file.
    assert ("hash_password", "src/auth/auth.py") in labels


def test_expand_pulls_in_callers_across_files(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    seed = _chunk_id(conn, "get_user_by_id", "src/users/service.py")

    added = expand_context(conn, [seed], max_hops=1, token_budget=4000)
    labels = _chunk_labels(conn, added)

    assert ("test_create_and_get_user", "tests/test_service.py") in labels
    assert ("test_delete_user", "tests/test_service.py") in labels


def test_expand_includes_the_files_module_chunk(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    seed = _chunk_id(conn, "create_user", "src/users/service.py")

    added = expand_context(conn, [seed], max_hops=1, token_budget=4000)
    labels = _chunk_labels(conn, added)
    assert (None, "src/users/service.py") in labels  # the module/imports chunk


def test_expand_never_returns_a_seed_chunk(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    seed = _chunk_id(conn, "create_user", "src/users/service.py")

    added = expand_context(conn, [seed], max_hops=2, token_budget=4000)
    assert seed not in added


def test_expand_respects_token_budget(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    seed = _chunk_id(conn, "create_user", "src/users/service.py")

    generous = expand_context(conn, [seed], max_hops=2, token_budget=4000)
    stingy = expand_context(conn, [seed], max_hops=2, token_budget=1)

    assert len(stingy) <= 1  # a budget this small always lets exactly one addition through
    assert len(stingy) < len(generous)


def test_expand_empty_seeds_returns_empty(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    assert expand_context(conn, [], max_hops=2, token_budget=4000) == []


def test_expand_unknown_chunk_id_is_skipped_not_raised(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    assert expand_context(conn, [999_999], max_hops=2, token_budget=4000) == []
