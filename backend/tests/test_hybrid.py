from __future__ import annotations

from pathlib import Path

from app.db import get_repo_connection
from app.index.lexical import index_chunk
from app.index.store import index_file
from app.index.vectors import embed_and_store_chunks
from app.ingest.walker import walk_repo
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.hybrid import hybrid_search

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


def test_hybrid_search_fuses_dense_and_lexical(tmp_path: Path):
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)

    auth_id = _insert_chunk(conn, "src/auth.py", "hash_password", "hash password salt pbkdf2 authentication")
    web_id = _insert_chunk(conn, "src/render.py", "render_html", "render html template view layout")
    conn.commit()

    embed_and_store_chunks(
        conn, provider, [(auth_id, "hash password salt"), (web_id, "render html template")], batch_size=256
    )
    index_chunk(conn, auth_id, content="hash password salt pbkdf2 authentication", symbol_name="hash_password", path="src/auth.py")
    index_chunk(conn, web_id, content="render html template view layout", symbol_name="render_html", path="src/render.py")
    conn.commit()

    results = hybrid_search(
        conn, provider, "how do we hash the password", top_k_dense=10, top_k_lexical=10, rrf_k=60, top_n=10
    )
    assert results, "expected at least one result"
    top_chunk_id, _score = results[0]
    assert top_chunk_id == auth_id


def test_hybrid_search_respects_top_n(tmp_path: Path):
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)

    chunk_ids = []
    to_embed = []
    for i in range(5):
        cid = _insert_chunk(conn, f"src/f{i}.py", f"func_{i}", f"function number {i} does something with data")
        chunk_ids.append(cid)
        to_embed.append((cid, f"function number {i} does something with data"))
        index_chunk(conn, cid, content=f"function number {i} does something with data", symbol_name=f"func_{i}", path=f"src/f{i}.py")
    conn.commit()
    embed_and_store_chunks(conn, provider, to_embed, batch_size=256)
    conn.commit()

    results = hybrid_search(conn, provider, "function data", top_k_dense=10, top_k_lexical=10, rrf_k=60, top_n=2)
    assert len(results) <= 2


def test_hybrid_search_ranks_appear_only_in_lexical_or_dense(tmp_path: Path):
    """A chunk that only matches lexically (exact identifier, no semantic
    overlap in the fake embedding space) should still surface via RRF."""
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)

    only_id = _insert_chunk(conn, "src/only.py", "veryUniqueIdentifierXyz", "veryUniqueIdentifierXyz does a thing")
    other_id = _insert_chunk(conn, "src/other.py", "other_func", "completely different content about nothing")
    conn.commit()
    embed_and_store_chunks(
        conn,
        provider,
        [(only_id, "veryUniqueIdentifierXyz does a thing"), (other_id, "completely different content about nothing")],
        batch_size=256,
    )
    index_chunk(conn, only_id, content="veryUniqueIdentifierXyz does a thing", symbol_name="veryUniqueIdentifierXyz", path="src/only.py")
    index_chunk(conn, other_id, content="completely different content about nothing", symbol_name="other_func", path="src/other.py")
    conn.commit()

    results = hybrid_search(
        conn, provider, "veryUniqueIdentifierXyz", top_k_dense=10, top_k_lexical=10, rrf_k=60, top_n=10
    )
    result_ids = [cid for cid, _score in results]
    assert only_id in result_ids


def _index_mini_repo(conn, provider, mini_repo_path: Path) -> None:
    """Runs the real ingest -> chunk -> embed -> lexical-index pipeline
    against the fixture repo, exactly as jobs.py does (minus the registry
    bookkeeping) — so retrieval tests exercise real chunk boundaries, not
    hand-crafted rows."""
    kept, _skipped = walk_repo(mini_repo_path)
    to_embed: list[tuple[int, str]] = []
    for discovered in kept:
        result = index_file(conn, discovered)
        if result.changed:
            rows = conn.execute(
                f"SELECT id, header, content FROM chunks WHERE id IN "
                f"({','.join('?' for _ in result.chunk_ids)})",
                result.chunk_ids,
            ).fetchall()
            for r in rows:
                text = f"{r['header']}\n{r['content']}" if r["header"] else r["content"]
                to_embed.append((r["id"], text))
    conn.commit()
    embed_and_store_chunks(conn, provider, to_embed, batch_size=256)
    conn.commit()


def test_exact_identifier_query_ranks_definition_in_top_3(mini_repo_path: Path, tmp_path: Path):
    """Phase 1 acceptance criterion: a query for an exact identifier
    (get_user_by_id) ranks that definition in the top 3."""
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)
    _index_mini_repo(conn, provider, mini_repo_path)

    results = hybrid_search(
        conn, provider, "get_user_by_id", top_k_dense=10, top_k_lexical=10, rrf_k=60, top_n=10
    )
    result_ids = [cid for cid, _score in results[:3]]
    rows = conn.execute(
        f"SELECT id FROM chunks WHERE symbol_name = 'get_user_by_id' AND id IN "
        f"({','.join('?' for _ in result_ids)})",
        result_ids,
    ).fetchall()
    assert rows, f"get_user_by_id not in top 3: {results[:3]}"


def test_natural_language_query_retrieves_the_right_file(mini_repo_path: Path, tmp_path: Path):
    """Phase 1 acceptance criterion: a natural-language query ('how do we
    hash passwords') retrieves the right file."""
    settings = make_settings(tmp_path)
    provider = FakeEmbeddingProvider()
    conn = get_repo_connection("repo1", provider.dim, settings)
    _index_mini_repo(conn, provider, mini_repo_path)

    results = hybrid_search(
        conn, provider, "how do we hash passwords", top_k_dense=10, top_k_lexical=10, rrf_k=60, top_n=10
    )
    result_ids = [cid for cid, _score in results]
    placeholders = ",".join("?" for _ in result_ids)
    rows = conn.execute(
        f"SELECT c.id, f.path FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id IN ({placeholders})",
        result_ids,
    ).fetchall()
    assert any(r["path"] == "src/auth/auth.py" for r in rows), [r["path"] for r in rows]
