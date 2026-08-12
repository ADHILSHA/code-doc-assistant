"""POST /repos, GET /repos, jobs, reindex, delete.

No business logic here — validate, delegate to app.jobs / app.db, serialize
(SPEC.md §7.6).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.config import Settings, get_settings
from app.db import get_registry_connection
from app.jobs import create_repo_and_job, run_index_job
from app.models import JobOut, RepoCreateRequest, RepoCreateResponse, RepoOut
from app.util import now_iso

router = APIRouter(tags=["repos"])


def registry_connection_dependency(
    settings: Settings = Depends(get_settings),
) -> Iterator[sqlite3.Connection]:
    """`settings` MUST be resolved via `Depends(get_settings)` here (not a
    bare default arg) — that's what lets `app.dependency_overrides[get_settings]`
    reach this connection in tests. A bare `Settings | None = None` default
    silently falls back to the real global settings instead, since FastAPI
    doesn't chain overrides through parameters it isn't asked to resolve."""
    conn = get_registry_connection(settings)
    try:
        yield conn
    finally:
        conn.close()


def _row_to_repo(row: sqlite3.Row) -> RepoOut:
    return RepoOut(
        id=row["id"],
        source=row["source"],
        source_type=row["source_type"],
        display_name=row["display_name"],
        status=row["status"],
        error=row["error"],
        commit_sha=row["commit_sha"],
        default_branch=row["default_branch"],
        stats=json.loads(row["stats_json"]) if row["stats_json"] else None,
        created_at=row["created_at"],
        indexed_at=row["indexed_at"],
    )


def _row_to_job(row: sqlite3.Row) -> JobOut:
    return JobOut(
        id=row["id"],
        repo_id=row["repo_id"],
        type=row["type"],
        state=row["state"],
        stage=row["stage"],
        progress=row["progress"],
        message=row["message"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("/repos", response_model=RepoCreateResponse, status_code=202)
def create_repo(
    body: RepoCreateRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> RepoCreateResponse:
    repo_id, job_id = create_repo_and_job(body.source, settings)
    background_tasks.add_task(run_index_job, job_id, repo_id, body.source, settings)
    return RepoCreateResponse(repo_id=repo_id, job_id=job_id)


@router.get("/repos", response_model=list[RepoOut])
def list_repos(conn: sqlite3.Connection = Depends(registry_connection_dependency)) -> list[RepoOut]:
    rows = conn.execute("SELECT * FROM repos ORDER BY created_at DESC").fetchall()
    return [_row_to_repo(r) for r in rows]


@router.get("/repos/{repo_id}", response_model=RepoOut)
def get_repo(
    repo_id: str, conn: sqlite3.Connection = Depends(registry_connection_dependency)
) -> RepoOut:
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "repo not found")
    return _row_to_repo(row)


@router.delete("/repos/{repo_id}", status_code=204)
def delete_repo(
    repo_id: str,
    conn: sqlite3.Connection = Depends(registry_connection_dependency),
    settings: Settings = Depends(get_settings),
) -> None:
    row = conn.execute("SELECT id FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "repo not found")
    conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
    conn.execute("DELETE FROM jobs WHERE repo_id = ?", (repo_id,))
    conn.commit()

    db_path = settings.repo_db_path(repo_id)
    for candidate in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
        if candidate.exists():
            candidate.unlink()
    clone_path = settings.repo_clone_path(repo_id)
    if clone_path.exists():
        shutil.rmtree(clone_path, ignore_errors=True)


@router.post("/repos/{repo_id}/reindex", status_code=202)
def reindex_repo(
    repo_id: str,
    background_tasks: BackgroundTasks,
    conn: sqlite3.Connection = Depends(registry_connection_dependency),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    row = conn.execute("SELECT source FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "repo not found")

    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs (id, repo_id, type, state, stage, progress, created_at, updated_at) "
        "VALUES (?, ?, 'reindex', 'queued', NULL, 0, ?, ?)",
        (job_id, repo_id, now_iso(), now_iso()),
    )
    conn.commit()
    background_tasks.add_task(run_index_job, job_id, repo_id, row["source"], settings)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, conn: sqlite3.Connection = Depends(registry_connection_dependency)) -> JobOut:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    return _row_to_job(row)
