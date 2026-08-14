"""Secret redaction wired into the real pipeline (SPEC.md §6 Phase 5 task 3
/ §7.5) — against `tests/fixtures/mini_repo/src/auth/legacy_key_notes.py`,
which deliberately contains fake credentials for exactly this purpose.

Regression coverage for a real bug found via a smoke test, not by
inspection: an earlier version of `redact_secrets` only scanned inside
quote-delimited literals, which silently missed a secret sitting inside a
*triple*-quoted docstring (a single-quote-anchored regex matches an empty
span between the first two characters of `\"\"\"`, never reaching the
docstring's actual content). Fixed to scan raw token-shaped runs instead;
`test_docstring_secret_is_redacted_in_symbols_table` below is the
regression test for that specific failure mode.
"""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.agent.tools import ToolContext, execute_tool
from app.db import get_registry_connection, get_repo_connection
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import MINI_REPO, make_settings

_FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_FAKE_GITHUB_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"
_FAKE_HIGH_ENTROPY_SECRET = "aX9kL2mQ8zR5vN1pT6wY3cJ7fH0dS4gK9mZ2"


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    return conn, repo_id, settings


def test_known_format_secrets_are_redacted_in_stored_chunks(tmp_path: Path):
    conn, _repo_id, _settings = _index_mini_repo(tmp_path)
    rows = conn.execute(
        "SELECT c.content FROM chunks c JOIN files f ON f.id = c.file_id "
        "WHERE f.path = 'src/auth/legacy_key_notes.py'"
    ).fetchall()
    all_content = " ".join(r["content"] for r in rows)

    assert _FAKE_AWS_KEY not in all_content
    assert _FAKE_GITHUB_TOKEN not in all_content
    assert "[REDACTED]" in all_content


def test_docstring_secret_is_redacted_in_symbols_table(tmp_path: Path):
    """Regression test — see module docstring."""
    conn, _repo_id, _settings = _index_mini_repo(tmp_path)
    row = conn.execute("SELECT docstring FROM symbols WHERE name = 'notes'").fetchone()
    assert row is not None
    assert _FAKE_HIGH_ENTROPY_SECRET not in row["docstring"]
    assert "[REDACTED]" in row["docstring"]


def test_redacted_content_is_not_sent_to_the_embedding_provider(tmp_path: Path):
    """The stored chunk is what gets embedded (index/vectors.py reads
    `chunks.content` back out to build the embedding request) — if
    storage is redacted, embedding necessarily is too. Confirms there's
    no separate un-redacted copy anywhere `chunk_vectors`/FTS derives from."""
    conn, _repo_id, _settings = _index_mini_repo(tmp_path)
    fts_rows = conn.execute(
        "SELECT content FROM chunks_fts WHERE content LIKE '%AKIAIOSFODNN7EXAMPLE%'"
    ).fetchall()
    assert fts_rows == []


def test_agent_read_file_tool_redacts_secrets(tmp_path: Path):
    conn, repo_id, settings = _index_mini_repo(tmp_path)
    registry_conn = get_registry_connection(settings)
    local_path = registry_conn.execute(
        "SELECT local_path FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["local_path"]
    ctx = ToolContext(
        conn=conn, repo_root=Path(local_path), embedding_provider=FakeEmbeddingProvider(), settings=settings
    )

    result, _summary = execute_tool("read_file", ctx, {"path": "src/auth/legacy_key_notes.py"})
    text = "\n".join(result["lines"])
    assert _FAKE_AWS_KEY not in text
    assert _FAKE_GITHUB_TOKEN not in text
    assert _FAKE_HIGH_ENTROPY_SECRET not in text
    assert "[REDACTED]" in text


def test_agent_grep_tool_redacts_secrets(tmp_path: Path):
    conn, repo_id, settings = _index_mini_repo(tmp_path)
    registry_conn = get_registry_connection(settings)
    local_path = registry_conn.execute(
        "SELECT local_path FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["local_path"]
    ctx = ToolContext(
        conn=conn, repo_root=Path(local_path), embedding_provider=FakeEmbeddingProvider(), settings=settings
    )

    result, _summary = execute_tool("grep", ctx, {"pattern": "LEGACY"})
    assert result  # the fixture file's matching lines were actually found
    text = str(result)
    assert _FAKE_AWS_KEY not in text
    assert _FAKE_GITHUB_TOKEN not in text
    assert "[REDACTED]" in text


def test_agent_grep_python_fallback_redacts_secrets(tmp_path: Path):
    """Same as above but forcing the non-ripgrep code path."""
    from app.agent import tools as tools_module

    conn, repo_id, settings = _index_mini_repo(tmp_path)
    registry_conn = get_registry_connection(settings)
    local_path = registry_conn.execute(
        "SELECT local_path FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["local_path"]
    ctx = ToolContext(
        conn=conn, repo_root=Path(local_path), embedding_provider=FakeEmbeddingProvider(), settings=settings
    )

    matches = tools_module._grep_python_fallback(ctx, "LEGACY", None, 50)
    assert matches
    text = str(matches)
    assert _FAKE_AWS_KEY not in text
    assert _FAKE_GITHUB_TOKEN not in text
    assert "[REDACTED]" in text
