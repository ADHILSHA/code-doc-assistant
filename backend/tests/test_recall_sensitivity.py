"""SPEC.md §6 Phase 4 acceptance criterion: "A deliberately degraded
retriever (dense-only) shows a measurably lower recall — proving the
harness is sensitive."

Operates directly at the retrieval layer (real `hybrid_search` vs. its
dense-only component, `index/vectors.py::query_top_k`) against the mini_repo
golden set's `expected_files` — the most direct, generation-independent way
to prove recall@k actually isolates retrieval quality (its whole purpose
per SPEC.md's metric description), rather than routing every question
through the full agent pipeline just to strip that signal back out again.
"""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.db import get_repo_connection
from app.eval.golden import golden_set_path, load_golden_set
from app.index.vectors import query_top_k
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.chunks import fetch_chunks
from app.retrieval.hybrid import hybrid_search

from .conftest import MINI_REPO, make_settings

_TOP_K = 10


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    return get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)


def _recall_at_k(conn, provider, questions, *, dense_only: bool) -> float:
    hits = 0
    scored = 0
    for q in questions:
        if not q.expected_files:
            continue
        scored += 1
        if dense_only:
            ranked = query_top_k(conn, provider, q.question, _TOP_K)
            chunk_ids = [cid for cid, _distance in ranked]
        else:
            ranked = hybrid_search(
                conn, provider, q.question,
                top_k_dense=_TOP_K, top_k_lexical=_TOP_K, rrf_k=60, top_n=_TOP_K,
            )
            chunk_ids = [cid for cid, _score in ranked]
        paths = {c.file_path for c in fetch_chunks(conn, chunk_ids)}
        if any(f in paths for f in q.expected_files):
            hits += 1
    return hits / scored if scored else 0.0


def test_dense_only_retriever_shows_measurably_lower_recall_than_hybrid(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    provider = FakeEmbeddingProvider()
    questions = load_golden_set(golden_set_path(Path(__file__).resolve().parents[2] / "eval" / "golden", "mini_repo"))

    hybrid_recall = _recall_at_k(conn, provider, questions, dense_only=False)
    dense_only_recall = _recall_at_k(conn, provider, questions, dense_only=True)

    # FakeEmbeddingProvider's vectors are hash-based, not semantically
    # meaningful (SPEC.md §7.2 — the only embedding provider tests may
    # use), so dense-only search on it is close to random; hybrid search's
    # lexical (BM25) half still matches exact identifiers golden questions
    # name directly ("UserService", "hash_password", ...). The gap is the
    # harness actually detecting a degraded retriever, not a fluke of one
    # run — both are deterministic given FakeEmbeddingProvider's hashing.
    assert hybrid_recall > dense_only_recall, (
        f"expected hybrid ({hybrid_recall:.2f}) to measurably beat dense-only ({dense_only_recall:.2f})"
    )
    assert hybrid_recall - dense_only_recall >= 0.2
