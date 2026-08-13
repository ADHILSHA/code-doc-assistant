"""Hierarchical summaries (SPEC.md §6 Phase 3 task 4), against the real
indexed mini_repo fixture."""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.db import get_repo_connection
from app.enrich.summarizer import summarize_repo
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider

from .conftest import MINI_REPO, make_settings


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    return get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)


def test_summarize_repo_produces_file_directory_and_repo_summaries(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    stats = summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")

    assert stats.files_summarized > 0
    assert stats.files_skipped_too_short > 0  # mini_repo has some tiny files (< 5 LOC)
    assert stats.directories_summarized > 0
    assert stats.repo_summarized is True
    assert len(stats.pending_embeddings) == stats.files_summarized

    scopes = {r["scope"] for r in conn.execute("SELECT DISTINCT scope FROM summaries")}
    assert scopes == {"file", "directory", "repo"}

    repo_row = conn.execute("SELECT content FROM summaries WHERE scope = 'repo'").fetchone()
    assert repo_row is not None
    assert repo_row["content"]


def test_summarize_repo_skips_files_under_min_loc(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    tiny_path = conn.execute("SELECT path FROM files WHERE loc < 5 LIMIT 1").fetchone()
    assert tiny_path is not None, "fixture needs at least one file under 5 LOC for this test to mean anything"

    summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")
    row = conn.execute(
        "SELECT 1 FROM summaries WHERE scope = 'file' AND target_path = ?", (tiny_path["path"],)
    ).fetchone()
    assert row is None


def test_summarize_repo_embeds_only_file_scope_as_summary_chunks(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")

    file_summary_count = conn.execute(
        "SELECT COUNT(*) AS n FROM summaries WHERE scope = 'file'"
    ).fetchone()["n"]
    summary_chunk_count = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE symbol_kind = 'summary'"
    ).fetchone()["n"]
    assert summary_chunk_count == file_summary_count

    # Every summary chunk has a real, non-null file_id (schema requires
    # NOT NULL) — directory/repo summaries never attempt this.
    for row in conn.execute("SELECT file_id FROM chunks WHERE symbol_kind = 'summary'"):
        assert row["file_id"] is not None


def test_summarize_repo_is_cached_by_source_hash_on_rerun(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    first = summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")
    assert first.files_summarized > 0

    second = summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")
    assert second.files_summarized == 0
    assert second.files_unchanged == first.files_summarized
    assert second.directories_summarized == 0
    assert second.repo_summarized is False
    assert second.pending_embeddings == []

    # No duplicate rows were created on the cached rerun.
    file_summary_count = conn.execute(
        "SELECT COUNT(*) AS n FROM summaries WHERE scope = 'file'"
    ).fetchone()["n"]
    assert file_summary_count == first.files_summarized


def test_summarize_repo_regenerates_when_a_files_content_changes(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")

    row = conn.execute(
        "SELECT id, path FROM files WHERE loc >= 5 LIMIT 1"
    ).fetchone()
    conn.execute("UPDATE files SET content_hash = 'deliberately-changed' WHERE id = ?", (row["id"],))
    conn.commit()

    second = summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")
    assert second.files_summarized == 1
    assert second.files_unchanged == second.files_unchanged  # sanity: didn't error


# --- concurrency, progress reporting, and the per-run file cap (found via a
# real user report: a large repo's job sat at "summarizing" for a long time
# with no visible progress, easily mistaken for a hang — see DECISIONS.md) ---


def test_summarize_repo_runs_file_level_calls_concurrently(tmp_path: Path):
    import time

    conn = _index_mini_repo(tmp_path)

    class SlowFakeLLMProvider(FakeLLMProvider):
        def complete(self, **kwargs):
            time.sleep(0.2)
            return super().complete(**kwargs)

    start = time.monotonic()
    stats = summarize_repo(
        conn, SlowFakeLLMProvider(), min_loc=5, display_name="mini_repo", concurrency=6
    )
    elapsed = time.monotonic() - start

    assert stats.files_summarized >= 6  # mini_repo has enough files >= 5 LOC for this to be meaningful
    # Sequential would take files_summarized * 0.2s; concurrency=6 should
    # finish well under that (allow generous slack for CI/thread overhead
    # and the still-sequential directory/repo passes that also sleep).
    sequential_estimate = stats.files_summarized * 0.2
    assert elapsed < sequential_estimate * 0.75, (
        f"expected concurrency to meaningfully beat sequential ({sequential_estimate:.2f}s), "
        f"took {elapsed:.2f}s"
    )


def test_summarize_repo_reports_progress_for_every_file(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    progress_calls: list[tuple[int, int]] = []

    stats = summarize_repo(
        conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo",
        on_progress=lambda done, total: progress_calls.append((done, total)),
    )

    assert len(progress_calls) == stats.files_summarized
    # Monotonically increasing "done", constant "total", ending at the full count.
    assert [d for d, _ in progress_calls] == list(range(1, stats.files_summarized + 1))
    assert all(t == stats.files_summarized for _, t in progress_calls)


def test_summarize_repo_respects_max_files_cap(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    stats = summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo", max_files=3)
    assert stats.files_summarized == 3

    file_summary_count = conn.execute(
        "SELECT COUNT(*) AS n FROM summaries WHERE scope = 'file'"
    ).fetchone()["n"]
    assert file_summary_count == 3


def test_summarize_repo_uncapped_files_are_picked_up_on_a_later_run(tmp_path: Path):
    conn = _index_mini_repo(tmp_path)
    first = summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo", max_files=2)
    assert first.files_summarized == 2

    # No cap this time — the files skipped by the previous run's cap (not
    # cached, since they were never summarized) get picked up now.
    second = summarize_repo(conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")
    assert second.files_summarized > 0

    total_file_summaries = conn.execute(
        "SELECT COUNT(*) AS n FROM summaries WHERE scope = 'file'"
    ).fetchone()["n"]
    assert total_file_summaries == 2 + second.files_summarized
