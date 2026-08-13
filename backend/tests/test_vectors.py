from __future__ import annotations

from pathlib import Path

from app.db import get_repo_connection
from app.index.vectors import embed_and_store_chunks, query_top_k
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import make_settings


def _insert_chunk(conn, file_path: str, content: str) -> int:
    file_row = conn.execute("SELECT id FROM files WHERE path = ?", (file_path,)).fetchone()
    if file_row is None:
        cur = conn.execute(
            "INSERT INTO files (path, language, content_hash, size_bytes, loc, is_test) "
            "VALUES (?, 'python', 'x', 0, 1, 0)",
            (file_path,),
        )
        file_id = cur.lastrowid
    else:
        file_id = file_row["id"]
    cur = conn.execute(
        "INSERT INTO chunks (file_id, start_line, end_line, header, content, token_count) "
        "VALUES (?, 1, 1, '', ?, 1)",
        (file_id, content),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_embed_and_query_top_k_ranks_relevant_chunk_first(tmp_path: Path):
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)

    auth_id = _insert_chunk(conn, "src/auth.py", "hash password salt pbkdf2 authentication")
    web_id = _insert_chunk(conn, "src/render.py", "render html template view layout")

    embed_and_store_chunks(
        conn, provider, [(auth_id, "hash password salt"), (web_id, "render html template")],
        batch_size=256,
    )

    results = query_top_k(conn, provider, "how do we hash the password", k=2)
    assert results, "expected at least one result"
    top_chunk_id, _distance = results[0]
    assert top_chunk_id == auth_id


def test_query_top_k_empty_index_returns_empty(tmp_path: Path):
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)
    assert query_top_k(conn, provider, "anything", k=5) == []


def test_query_top_k_zero_k_returns_empty(tmp_path: Path):
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)
    assert query_top_k(conn, provider, "anything", k=0) == []
