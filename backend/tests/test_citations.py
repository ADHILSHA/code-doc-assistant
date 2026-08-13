from __future__ import annotations

from pathlib import Path

from app.db import get_repo_connection
from app.generation.citations import (
    RepoContext,
    attach_permalinks,
    build_permalink,
    verify_citations,
)
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import make_settings


def _seed_file(conn, path: str, loc: int) -> None:
    conn.execute(
        "INSERT INTO files (path, language, content_hash, size_bytes, loc, is_test) "
        "VALUES (?, 'python', 'x', 0, ?, 0)",
        (path, loc),
    )
    conn.commit()


def test_verify_citations_all_valid(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    _seed_file(conn, "src/auth.py", 30)

    answer = "Passwords are hashed with PBKDF2 [src/auth.py:1-21]. Verification is constant-time [src/auth.py:24-30]."
    result = verify_citations(conn, answer)

    assert result.invalid_count == 0
    assert result.unsupported_claim is False
    assert [(c.path, c.start_line, c.end_line) for c in result.citations] == [
        ("src/auth.py", 1, 21),
        ("src/auth.py", 24, 30),
    ]
    assert [c.id for c in result.citations] == [1, 2]


def test_verify_citations_strips_invalid_path(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    _seed_file(conn, "src/auth.py", 30)

    answer = "Rate limiting happens here [src/nope.py:1-5]."
    result = verify_citations(conn, answer)

    assert result.citations == []
    assert result.invalid_count == 1
    assert result.unsupported_claim is True  # the claim's only citation was invalid


def test_verify_citations_strips_out_of_bounds_line_range(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    _seed_file(conn, "src/auth.py", 30)

    answer = "This claim cites past the end of the file [src/auth.py:100-200]."
    result = verify_citations(conn, answer)

    assert result.citations == []
    assert result.invalid_count == 1
    assert result.unsupported_claim is True


def test_verify_citations_claim_with_one_valid_and_one_invalid_citation_is_supported(tmp_path: Path):
    """A claim isn't 'unsupported' as long as at least one of its citations
    checks out — only strip the invalid one."""
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    _seed_file(conn, "src/auth.py", 30)

    answer = "Hashing uses PBKDF2 [src/auth.py:1-21] [src/nope.py:1-5]."
    result = verify_citations(conn, answer)

    assert len(result.citations) == 1
    assert result.citations[0].path == "src/auth.py"
    assert result.invalid_count == 1
    assert result.unsupported_claim is False


def test_verify_citations_no_citations_at_all(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    _seed_file(conn, "src/auth.py", 30)

    result = verify_citations(conn, "There is no citation in this answer at all.")
    assert result.citations == []
    assert result.invalid_count == 0
    assert result.unsupported_claim is False


def test_build_permalink_github():
    repo = RepoContext(source_type="github", owner_repo="acme/widgets", commit_sha="abc123")
    assert (
        build_permalink(repo, "src/auth.py", 10, 20)
        == "https://github.com/acme/widgets/blob/abc123/src/auth.py#L10-L20"
    )
    # Single-line citation uses a single line fragment, not a range.
    assert build_permalink(repo, "src/auth.py", 10, 10) == "https://github.com/acme/widgets/blob/abc123/src/auth.py#L10"


def test_build_permalink_local_repo_returns_none():
    repo = RepoContext(source_type="local", owner_repo=None, commit_sha=None)
    assert build_permalink(repo, "src/auth.py", 10, 20) is None


def test_attach_permalinks(tmp_path: Path):
    settings = make_settings(tmp_path)
    conn = get_repo_connection("repo1", FakeEmbeddingProvider().dim, settings)
    _seed_file(conn, "src/auth.py", 30)

    result = verify_citations(conn, "Hashing uses PBKDF2 [src/auth.py:1-21].")
    repo = RepoContext(source_type="github", owner_repo="acme/widgets", commit_sha="abc123")
    with_urls = attach_permalinks(result.citations, repo)
    assert with_urls[0].url == "https://github.com/acme/widgets/blob/abc123/src/auth.py#L1-L21"
