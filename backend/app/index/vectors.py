"""Embedding + sqlite-vec ops: batch-embed chunks into `chunk_vectors`,
dense top-k query.
"""

from __future__ import annotations

import math
import sqlite3

import sqlite_vec

from app.providers.embeddings import EmbeddingProvider


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_and_store_chunks(
    conn: sqlite3.Connection,
    provider: EmbeddingProvider,
    chunks: list[tuple[int, str]],
    *,
    batch_size: int,
) -> None:
    """Batch-embed `chunks` (chunk_id, text_to_embed) and upsert into
    `chunk_vectors`, `batch_size` at a time (SPEC.md §7.4).

    Vectors are L2-normalized before storage so vec0's default L2-distance
    search ranks identically to cosine similarity — see `query_top_k`.
    """
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        vectors = provider.embed_documents([text for _, text in batch])
        for (chunk_id, _), vec in zip(batch, vectors):
            conn.execute(
                "INSERT OR REPLACE INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, sqlite_vec.serialize_float32(_normalize(vec))),
            )
    conn.commit()


def query_top_k(
    conn: sqlite3.Connection, provider: EmbeddingProvider, query_text: str, k: int
) -> list[tuple[int, float]]:
    """Dense top-k search, nearest-first, as (chunk_id, distance).

    For unit vectors, L2^2 = 2 - 2*cos_sim, so minimizing L2 distance is
    equivalent to maximizing cosine similarity — normalizing at both write
    and query time makes vec0's default L2 metric behave as cosine without
    needing a custom distance function.
    """
    if k <= 0:
        return []
    query_vec = _normalize(provider.embed_query(query_text))
    rows = conn.execute(
        "SELECT chunk_id, distance FROM chunk_vectors WHERE embedding MATCH ? AND k = ? "
        "ORDER BY distance",
        (sqlite_vec.serialize_float32(query_vec), k),
    ).fetchall()
    return [(r["chunk_id"], r["distance"]) for r in rows]
