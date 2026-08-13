from __future__ import annotations

from pathlib import Path

from app.db import get_repo_connection
from app.index import lexical
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import make_settings


def _insert_chunk(conn, file_path: str, symbol_name: str, content: str) -> int:
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
        "INSERT INTO chunks (file_id, symbol_name, start_line, end_line, header, content, token_count) "
        "VALUES (?, ?, 1, 1, '', ?, 1)",
        (file_id, symbol_name, content),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_split_identifier():
    assert lexical.split_identifier("getUserById") == "get user by id"
    assert lexical.split_identifier("get_user_by_id") == "get user by id"
    assert lexical.split_identifier("HTTPServer") == "http server"
    assert lexical.split_identifier("UserService") == "user service"
    assert lexical.split_identifier("simple") == "simple"


def test_index_and_search_exact_identifier(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    chunk_id = _insert_chunk(conn, "src/service.py", "get_user_by_id", "def get_user_by_id(id): ...")
    lexical.index_chunk(conn, chunk_id, content="def get_user_by_id(id): ...", symbol_name="get_user_by_id", path="src/service.py")
    conn.commit()

    assert lexical.bm25_search(conn, "get_user_by_id", k=5) == [chunk_id]


def test_search_matches_natural_language_split_of_identifier(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    chunk_id = _insert_chunk(conn, "src/auth.py", "hashPassword", "def hashPassword(pw): ...")
    lexical.index_chunk(conn, chunk_id, content="def hashPassword(pw): ...", symbol_name="hashPassword", path="src/auth.py")
    conn.commit()

    # Exact identifier and its natural-language split both hit the same row.
    assert lexical.bm25_search(conn, "hashPassword", k=5) == [chunk_id]
    assert lexical.bm25_search(conn, "hash password", k=5) == [chunk_id]


def test_search_no_match_returns_empty(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    assert lexical.bm25_search(conn, "totally unrelated query xyz", k=5) == []


def test_search_k_zero_returns_empty(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    assert lexical.bm25_search(conn, "anything", k=0) == []


def test_delete_chunk_removes_it_from_search(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    chunk_id = _insert_chunk(conn, "src/a.py", "foo", "def foo(): pass")
    lexical.index_chunk(conn, chunk_id, content="def foo(): pass", symbol_name="foo", path="src/a.py")
    conn.commit()
    assert lexical.bm25_search(conn, "foo", k=5) == [chunk_id]

    lexical.delete_chunk(conn, chunk_id)
    conn.commit()
    assert lexical.bm25_search(conn, "foo", k=5) == []


def test_index_chunk_replaces_existing_row(tmp_path: Path):
    """Re-indexing the same chunk id (e.g. after a content edit within the
    same row) must not leave a stale duplicate entry behind."""
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    chunk_id = _insert_chunk(conn, "src/a.py", "foo", "def foo(): return 1")
    lexical.index_chunk(conn, chunk_id, content="def foo(): return 1", symbol_name="foo", path="src/a.py")
    conn.commit()

    lexical.index_chunk(conn, chunk_id, content="def foo(): return 2", symbol_name="foo", path="src/a.py")
    conn.commit()

    count = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts WHERE rowid = ?", (chunk_id,)).fetchone()["n"]
    assert count == 1
