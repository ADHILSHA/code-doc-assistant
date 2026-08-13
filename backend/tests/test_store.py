from __future__ import annotations

import hashlib
from pathlib import Path

import sqlite_vec

from app.db import get_repo_connection
from app.index.store import index_file
from app.ingest.walker import DiscoveredFile, walk_repo
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import make_settings


def _discovered(path: str, text: str, *, language: str = "python", is_test: bool = False) -> DiscoveredFile:
    return DiscoveredFile(
        path=path,
        language=language,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        size_bytes=len(text.encode("utf-8")),
        loc=len(text.splitlines()),
        is_test=is_test,
        text=text,
    )


def test_index_file_is_idempotent(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    discovered = _discovered("src/a.py", "def f():\n    return 1\n")

    first = index_file(conn, discovered)
    assert first.changed is True
    assert first.chunking_strategy == "ast"
    assert len(first.chunk_ids) >= 1

    second = index_file(conn, discovered)
    assert second.changed is False
    assert second.chunking_strategy is None
    assert second.chunk_ids == first.chunk_ids

    file_count = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    chunk_count = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    assert file_count == 1
    assert chunk_count == len(first.chunk_ids)


def test_index_file_replaces_chunks_vectors_and_lexical_rows_on_change(tmp_path: Path):
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)

    v1 = _discovered("src/a.py", "def f():\n    return 1\n")
    result1 = index_file(conn, v1)
    # Pretend these chunks got embedded and lexically indexed.
    for chunk_id in result1.chunk_ids:
        conn.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(provider.embed_query("x"))),
        )
    conn.commit()
    fts_count_before = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
    assert fts_count_before == len(result1.chunk_ids)

    v2 = _discovered("src/a.py", "def f():\n    return 2\n\ndef g():\n    return 3\n")
    result2 = index_file(conn, v2)
    assert result2.changed is True
    assert result2.file_id == result1.file_id
    assert set(result2.chunk_ids).isdisjoint(result1.chunk_ids)

    remaining_old_vectors = conn.execute(
        f"SELECT COUNT(*) AS n FROM chunk_vectors WHERE chunk_id IN "
        f"({','.join('?' for _ in result1.chunk_ids)})",
        result1.chunk_ids,
    ).fetchone()["n"]
    assert remaining_old_vectors == 0

    fts_count_after = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
    assert fts_count_after == len(result2.chunk_ids)


def test_index_file_from_real_walker(mini_repo_path: Path, tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    kept, _ = walk_repo(mini_repo_path)
    by_path = {f.path: f for f in kept}
    result = index_file(conn, by_path["src/users/service.py"])
    assert result.changed
    assert result.chunking_strategy == "ast"
    assert len(result.chunk_ids) >= 1

    rows = conn.execute(
        f"SELECT symbol_name, content FROM chunks WHERE id IN "
        f"({','.join('?' for _ in result.chunk_ids)})",
        result.chunk_ids,
    ).fetchall()
    assert any(r["symbol_name"] == "get_user_by_id" for r in rows)


def test_index_file_header_includes_path_and_class(mini_repo_path: Path, tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    kept, _ = walk_repo(mini_repo_path)
    by_path = {f.path: f for f in kept}
    result = index_file(conn, by_path["src/users/service.py"])

    rows = conn.execute(
        f"SELECT symbol_name, parent_symbol, header FROM chunks WHERE id IN "
        f"({','.join('?' for _ in result.chunk_ids)})",
        result.chunk_ids,
    ).fetchall()
    method_row = next(r for r in rows if r["symbol_name"] == "get_user_by_id")
    assert "src/users/service.py" in method_row["header"]
    assert method_row["parent_symbol"] == "UserService"
    assert "UserService" in method_row["header"]


def test_index_file_naive_fallback_for_unsupported_language(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    discovered = _discovered("src/a.php", "<?php\nfunction foo() { return 1; }\n", language="php")
    result = index_file(conn, discovered)
    assert result.chunking_strategy == "naive"
