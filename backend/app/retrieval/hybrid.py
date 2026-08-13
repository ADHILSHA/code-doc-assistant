"""Hybrid retrieval: dense (index/vectors.py) + lexical (index/lexical.py)
BM25, fused with Reciprocal Rank Fusion (SPEC.md §6 Phase 1 task 3).
"""

from __future__ import annotations

import sqlite3

from app.index.lexical import bm25_search
from app.index.vectors import query_top_k
from app.providers.embeddings import EmbeddingProvider


def hybrid_search(
    conn: sqlite3.Connection,
    provider: EmbeddingProvider,
    query: str,
    *,
    top_k_dense: int,
    top_k_lexical: int,
    rrf_k: int,
    top_n: int,
) -> list[tuple[int, float]]:
    """Best-first (chunk_id, rrf_score) pairs, fusing the dense and lexical
    rankings via Reciprocal Rank Fusion: each ranked list contributes
    `1 / (rrf_k + rank)` per chunk (1-indexed rank), summed across lists.
    """
    dense_ranked = [chunk_id for chunk_id, _distance in query_top_k(conn, provider, query, top_k_dense)]
    lexical_ranked = bm25_search(conn, query, top_k_lexical)

    scores: dict[int, float] = {}
    for rank, chunk_id in enumerate(dense_ranked, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
    for rank, chunk_id in enumerate(lexical_ranked, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return ranked[:top_n]
