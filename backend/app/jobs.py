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

from app.config import Settings, get_settings
from app.db import get_registry_connection, get_repo_connection
from app.index.store import index_file
from app.index.vectors import embed_and_store_chunks
from app.ingest.source import SourceError, classify_source, resolve_source
from app.ingest.walker import walk_repo
from app.providers.embeddings import get_embedding_provider
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


def run_index_job(job_id: str, repo_id: str, source: str, settings: Settings | None = None) -> None:
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
            total_chunks_written = 0
            pending_embeddings: list[tuple[int, str]] = []
            total_files = len(kept)
            # Which chunking strategy each (re)chunked file used, and which
            # languages fell back to the naive splitter — SPEC.md §6 Phase 1
            # task 1: "log which languages fell back".
            chunking_counts: dict[str, int] = {}
            fallback_languages: set[str] = set()

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

            _update_repo(registry_conn, repo_id, status="embedding")
            _update_job(
                registry_conn, job_id, stage="embedding", progress=0.55,
                message=f"Embedding {len(pending_embeddings)} chunks",
            )

            embed_and_store_chunks(
                repo_conn, embedding_provider, pending_embeddings,
                batch_size=settings.embedding_batch_size,
            )

            stats = {
                "files": total_files,
                "files_skipped": len(skipped),
                "chunks": total_chunks_written,
                "chunks_embedded": len(pending_embeddings),
                "languages": sorted({f.language for f in kept if f.language}),
                "chunking": chunking_counts,
                "fallback_languages": sorted(fallback_languages),
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
