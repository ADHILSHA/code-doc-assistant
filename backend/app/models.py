"""Pydantic schemas shared across layers (API request/response bodies,
and plain data carried between ingest/index/retrieval/generation)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SourceType = Literal["github", "local"]
RepoStatus = Literal["pending", "cloning", "parsing", "embedding", "ready", "failed"]
JobType = Literal["index", "reindex"]
JobState = Literal["queued", "running", "succeeded", "failed"]


# --- /api/repos ---


class RepoCreateRequest(BaseModel):
    source: str = Field(..., description="GitHub URL or local filesystem path")


class RepoCreateResponse(BaseModel):
    repo_id: str
    job_id: str


class RepoOut(BaseModel):
    id: str
    source: str
    source_type: SourceType
    display_name: str
    status: RepoStatus
    error: str | None = None
    commit_sha: str | None = None
    default_branch: str | None = None
    stats: dict[str, Any] | None = None
    created_at: str
    indexed_at: str | None = None


# --- /api/jobs ---


class JobOut(BaseModel):
    id: str
    repo_id: str
    type: JobType
    state: JobState
    stage: str | None = None
    progress: float = 0.0
    message: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


# --- /api/query ---


class QueryRequest(BaseModel):
    repo_id: str
    question: str
    session_id: str | None = None


# --- /api/repos/{id}/file ---


class FileSliceResponse(BaseModel):
    path: str
    language: str | None = None
    start_line: int
    end_line: int
    lines: list[str]


# --- internal: retrieval / generation ---


class RetrievedChunk(BaseModel):
    """A chunk returned by retrieval, carried through to context assembly."""

    chunk_id: int
    file_path: str
    symbol_name: str | None = None
    symbol_kind: str | None = None
    start_line: int
    end_line: int
    content: str
    header: str | None = None
    score: float = 0.0


class Citation(BaseModel):
    id: int
    path: str
    start_line: int
    end_line: int
    url: str | None = None
