"""Background indexing job: registry bookkeeping (`repos`/`jobs` rows +
progress) around the ingest → chunk → embed pipeline.

Invoked via FastAPI `BackgroundTasks` (a sync function added that way runs
in a threadpool, so it doesn't block the event loop — see api/repos.py).
"""

from __future__ import annotations

import json
import sqlite3
import traceback
import uuid
from collections.abc import Callable

from app.config import Settings, get_settings
from app.db import get_registry_connection, get_repo_connection
from app.enrich.summarizer import SummaryStats, summarize_repo
from app.index.graph_store import PendingFile, index_file_graph, resolve_and_write_refs
from app.index.store import index_file
from app.index.vectors import embed_and_store_chunks
from app.ingest.incremental import diff_files, remove_stale_files
from app.ingest.source import SourceError, classify_source, resolve_source
from app.ingest.walker import walk_repo
from app.providers.embeddings import EmbeddingProvider, get_embedding_provider
from app.providers.llm import LLMProvider, get_summarization_llm_provider
from app.util import now_iso


def create_repo_and_job(source: str, settings: Settings | None = None) -> tuple[str, str]:
    """Registers a pending repo + queued index job, ready to hand off to
    `run_index_job`. Returns (repo_id, job_id)."""
    settings = settings or get_settings()
    repo_id = uuid.uuid4().hex
    job_id = uuid.uuid4().hex

    conn = get_registry_connection(settings)
    try:
        conn.execute(
            "INSERT INTO repos (id, source, source_type, display_name, local_path, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (repo_id, source, classify_source(source), source, "", now_iso()),
        )
        conn.execute(
            "INSERT INTO jobs (id, repo_id, type, state, stage, progress, created_at, updated_at) "
            "VALUES (?, ?, 'index', 'queued', NULL, 0, ?, ?)",
            (job_id, repo_id, now_iso(), now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return repo_id, job_id


def run_index_job(
    job_id: str,
    repo_id: str,
    source: str,
    settings: Settings | None = None,
    summarization_llm_provider: LLMProvider | None = None,
) -> None:
    """`summarization_llm_provider` is normally left None (resolved from
    `settings` below) — the override exists so tests can inject a
    `FakeLLMProvider` and exercise Phase 3 summarization through the real
    indexing pipeline without an ANTHROPIC_API_KEY, the same pattern
    `EmbeddingProvider`/`LLMProvider` selection already uses elsewhere."""
    settings = settings or get_settings()
    registry_conn = get_registry_connection(settings)
    try:
        _update_job(
            registry_conn, job_id, state="running", stage="cloning", progress=0.0,
            message="Resolving source",
        )
        _update_repo(registry_conn, repo_id, status="cloning")

        resolved = resolve_source(source, repo_id, settings)

        _update_repo(
            registry_conn,
            repo_id,
            local_path=str(resolved.local_path),
            commit_sha=resolved.commit_sha,
            default_branch=resolved.default_branch,
            display_name=resolved.display_name,
            status="parsing",
        )
        _update_job(registry_conn, job_id, stage="parsing", progress=0.15, message="Walking files")

        kept, skipped = walk_repo(resolved.local_path)

        embedding_provider = get_embedding_provider(settings)
        repo_conn = get_repo_connection(repo_id, embedding_provider.dim, settings)
        try:
            # SPEC.md §6 Phase 4 task 4: computed against `files` as it
            # stood *before* this run touches it — index_file() below
            # already skips re-chunking `diff.unchanged`/only-content-hash-
            # identical files, but nothing yet knows about `diff.removed`
            # (files gone from the repo since the last index) until this.
            diff = diff_files(repo_conn, kept)

            total_chunks_written = 0
            pending_embeddings: list[tuple[int, str]] = []
            total_files = len(kept)
            # Which chunking strategy each (re)chunked file used, and which
            # languages fell back to the naive splitter — SPEC.md §6 Phase 1
            # task 1: "log which languages fell back".
            chunking_counts: dict[str, int] = {}
            fallback_languages: set[str] = set()
            # Phase 2 (SPEC.md §6): symbols/endpoints/dependencies are written
            # per file below (they need no cross-file information); calls and
            # imports are staged here and resolved into symbol_refs/
            # import_edges once, after every file's symbols have been
            # written — see index/graph_store.py's module docstring for why
            # this has to be two-phase.
            pending_graph_files: list[PendingFile] = []

            for idx, discovered in enumerate(kept):
                result = index_file(
                    repo_conn,
                    discovered,
                    max_tokens=settings.chunk_max_tokens,
                    overlap_statements=settings.chunk_overlap_statements,
                    naive_size=settings.chunk_size_chars,
                    naive_overlap=settings.chunk_overlap_chars,
                )
                total_chunks_written += len(result.chunk_ids)

                if result.changed:
                    pending_graph_files.append(
                        index_file_graph(
                            repo_conn, result.file_id, discovered.path, discovered.text,
                            discovered.language,
                        )
                    )

                if total_chunks_written > settings.max_chunks_per_repo:
                    raise RuntimeError(
                        f"Repo exceeds MAX_CHUNKS_PER_REPO ({settings.max_chunks_per_repo}); "
                        "refusing to index further"
                    )

                if result.chunking_strategy is not None:
                    chunking_counts[result.chunking_strategy] = (
                        chunking_counts.get(result.chunking_strategy, 0) + 1
                    )
                    if result.chunking_strategy == "naive" and discovered.language:
                        fallback_languages.add(discovered.language)

                if result.changed and result.chunk_ids:
                    placeholders = ",".join("?" for _ in result.chunk_ids)
                    rows = repo_conn.execute(
                        f"SELECT id, header, content FROM chunks WHERE id IN ({placeholders})",
                        result.chunk_ids,
                    ).fetchall()
                    for r in rows:
                        text = f"{r['header']}\n{r['content']}" if r["header"] else r["content"]
                        pending_embeddings.append((r["id"], text))

                if idx % 20 == 0 or idx == total_files - 1:
                    progress = 0.15 + 0.35 * ((idx + 1) / max(total_files, 1))
                    _update_job(
                        registry_conn, job_id, progress=progress,
                        message=f"Parsed {idx + 1}/{total_files} files",
                    )

            _update_job(
                registry_conn, job_id, stage="linking", progress=0.5,
                message="Resolving symbol references and imports",
            )
            resolve_and_write_refs(repo_conn, pending_graph_files)

            _update_repo(registry_conn, repo_id, status="embedding")
            _update_job(
                registry_conn, job_id, stage="embedding", progress=0.55,
                message=f"Embedding {len(pending_embeddings)} chunks",
            )

            embed_and_store_chunks(
                repo_conn, embedding_provider, pending_embeddings,
                batch_size=settings.embedding_batch_size,
            )

            _update_job(
                registry_conn, job_id, stage="summarizing", progress=0.9,
                message="Generating hierarchical summaries",
            )

            def _on_summary_progress(done: int, total: int) -> None:
                # Reserve 0.90-0.98 for the (usually dominant) file-level
                # pass; directory/repo-level summaries fill the rest. Without
                # this, a repo with many files sat at a single static
                # "summarizing" message for however long the whole stage
                # took — indistinguishable from a hung job.
                progress = 0.9 + 0.08 * (done / max(total, 1))
                _update_job(
                    registry_conn, job_id, progress=progress,
                    message=f"Summarizing files ({done}/{total})",
                )

            summary_stats = _run_summarization(
                repo_conn, embedding_provider, settings, resolved.display_name,
                summarization_llm_provider, _on_summary_progress,
            )

            # SPEC.md §6 Phase 4 task 4: "delete orphaned chunks, symbols,
            # refs, and vectors" for files no longer in the repo. Done last
            # (not interleaved with the main loop above) so it can't race
            # with anything still reading `files` by path during indexing.
            files_removed = remove_stale_files(repo_conn, diff.removed)

            symbols_count = repo_conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
            endpoints_count = repo_conn.execute("SELECT COUNT(*) AS n FROM endpoints").fetchone()["n"]
            dependencies_count = repo_conn.execute("SELECT COUNT(*) AS n FROM dependencies").fetchone()["n"]

            stats = {
                "files": total_files,
                "files_skipped": len(skipped),
                "files_added": len(diff.added),
                "files_changed": len(diff.changed),
                "files_unchanged": len(diff.unchanged),
                "files_removed": files_removed,
                "chunks": total_chunks_written,
                "chunks_embedded": len(pending_embeddings),
                "languages": sorted({f.language for f in kept if f.language}),
                "chunking": chunking_counts,
                "fallback_languages": sorted(fallback_languages),
                "symbols": symbols_count,
                "endpoints": endpoints_count,
                "dependencies": dependencies_count,
                "summaries": (
                    {
                        "files": summary_stats.files_summarized,
                        "files_skipped_too_short": summary_stats.files_skipped_too_short,
                        "directories": summary_stats.directories_summarized,
                        "repo": summary_stats.repo_summarized,
                    }
                    if summary_stats is not None
                    else None  # skipped — no ANTHROPIC_API_KEY available
                ),
            }
        finally:
            repo_conn.close()

        _update_repo(
            registry_conn, repo_id, status="ready", stats_json=json.dumps(stats),
            indexed_at=now_iso(),
        )
        _update_job(
            registry_conn, job_id, state="succeeded", stage="ready", progress=1.0,
            message="Index complete",
        )
    except SourceError as exc:
        _fail(registry_conn, job_id, repo_id, str(exc))
    except Exception as exc:  # noqa: BLE001 - job failures must be recorded, not raised into a thread pool
        _fail(registry_conn, job_id, repo_id, f"{exc}\n{traceback.format_exc()}")
    finally:
        registry_conn.close()


def _run_summarization(
    repo_conn: sqlite3.Connection,
    embedding_provider: EmbeddingProvider,
    settings: Settings,
    display_name: str,
    llm_provider_override: LLMProvider | None,
    on_progress: Callable[[int, int], None] | None = None,
) -> SummaryStats | None:
    """None means "skipped" (no ANTHROPIC_API_KEY) — hierarchical
    summaries are an enrichment on top of a successful index, not required
    for indexing itself to succeed (SPEC.md §6 Phase 3 task 4), so a
    missing LLM key here degrades the `overview` route gracefully (falls
    back to the agent path — see generation/answer.py) rather than failing
    the whole indexing job.

    File-level summaries run `settings.summary_concurrency` at a time and
    are capped at `settings.summary_max_files_per_run` — see
    `enrich/summarizer.py::summarize_repo`'s docstring for why: a fully
    sequential, uncapped pass could leave the job sitting at "summarizing"
    for a very long time on a large repo with no visible progress in
    between, easily mistaken for a hang.
    """
    llm_provider = llm_provider_override
    if llm_provider is None:
        try:
            llm_provider = get_summarization_llm_provider(settings)
        except RuntimeError:
            return None

    stats = summarize_repo(
        repo_conn, llm_provider, min_loc=settings.summary_min_loc, display_name=display_name,
        concurrency=settings.summary_concurrency, max_files=settings.summary_max_files_per_run,
        on_progress=on_progress,
    )
    if stats.pending_embeddings:
        embed_and_store_chunks(
            repo_conn, embedding_provider, stats.pending_embeddings, batch_size=settings.embedding_batch_size
        )
    return stats


def _fail(registry_conn: sqlite3.Connection, job_id: str, repo_id: str, message: str) -> None:
    _update_job(registry_conn, job_id, state="failed", error=message, message="Indexing failed")
    _update_repo(registry_conn, repo_id, status="failed", error=message)


def _update_job(conn: sqlite3.Connection, job_id: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
    conn.commit()


def _update_repo(conn: sqlite3.Connection, repo_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE repos SET {cols} WHERE id = ?", (*fields.values(), repo_id))
    conn.commit()
