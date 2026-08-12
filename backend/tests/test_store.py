from __future__ import annotations

import hashlib
from itertools import pairwise
from pathlib import Path

import sqlite_vec

from app.config import Settings
from app.db import get_repo_connection
from app.index.store import index_file, naive_chunk_text
from app.ingest.walker import DiscoveredFile, walk_repo
from app.providers.embeddings import FakeEmbeddingProvider


def _discovered(path: str, text: str, *, is_test: bool = False) -> DiscoveredFile:
    return DiscoveredFile(
        path=path,
        language="python",
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        size_bytes=len(text.encode("utf-8")),
        loc=len(text.splitlines()),
        is_test=is_test,
        text=text,
    )


def test_naive_chunk_text_small_text_single_chunk():
    text = "line1\nline2\nline3\n"
    chunks = naive_chunk_text(text, size=1500, overlap=200)
    assert len(chunks) == 1
    start_line, end_line, content = chunks[0]
    assert start_line == 1
    assert end_line == text.count("\n")  # 3 newline-terminated lines, no trailing phantom line
    assert content == text


def test_naive_chunk_text_empty():
    assert naive_chunk_text("") == []


def test_naive_chunk_text_large_text_overlaps_and_covers_whole_file():
    text = "".join(f"line {i}\n" for i in range(1000))  # well over 1500 chars
    chunks = naive_chunk_text(text, size=1500, overlap=200)
    assert len(chunks) > 1
    # Every window after the first should start before the previous one ends
    # (i.e. there is overlap), and the last window reaches the final line.
    for (_, e1, _), (s2, _, _) in pairwise(chunks):
        assert s2 <= e1
    total_lines = text.count("\n")
    assert chunks[-1][1] == total_lines


def test_index_file_is_idempotent(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", embedding_provider="fake")
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    discovered = _discovered("src/a.py", "def f():\n    return 1\n")

    first = index_file(conn, discovered)
    assert first.changed is True
    assert len(first.chunk_ids) >= 1

    second = index_file(conn, discovered)
    assert second.changed is False
    assert second.chunk_ids == first.chunk_ids

    file_count = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    chunk_count = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    assert file_count == 1
    assert chunk_count == len(first.chunk_ids)


def test_index_file_replaces_chunks_and_vectors_on_change(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", embedding_provider="fake")
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)

    v1 = _discovered("src/a.py", "def f():\n    return 1\n")
    result1 = index_file(conn, v1)
    # Pretend this chunk got embedded.
    for chunk_id in result1.chunk_ids:
        conn.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(provider.embed_query("x"))),
        )
    conn.commit()

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


def test_index_file_from_real_walker(mini_repo_path: Path, tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", embedding_provider="fake")
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)

    kept, _ = walk_repo(mini_repo_path)
    by_path = {f.path: f for f in kept}
    result = index_file(conn, by_path["src/users/service.py"])
    assert result.changed
    assert len(result.chunk_ids) >= 1

    row = conn.execute(
        "SELECT content FROM chunks WHERE id = ?", (result.chunk_ids[0],)
    ).fetchone()
    assert "get_user_by_id" in row["content"] or "UserService" in row["content"]
