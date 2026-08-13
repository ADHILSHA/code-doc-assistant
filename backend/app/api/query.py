"""POST /query (SSE stream). No business logic here — validate, delegate to
app.generation.answer, serialize (SPEC.md §7.6).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from typing import cast

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.config import Settings, get_settings
from app.db import get_registry_connection, get_repo_connection
from app.generation.answer import generate_answer_events
from app.generation.citations import RepoContext
from app.models import QueryRequest
from app.providers.embeddings import EmbeddingProvider, get_embedding_provider
from app.providers.llm import LLMProvider, get_llm_provider

router = APIRouter(tags=["query"])

_SENTINEL = object()


def registry_connection_dependency(
    settings: Settings = Depends(get_settings),
):
    """See api/repos.py::registry_connection_dependency for why `settings`
    must come through `Depends(get_settings)` here."""
    conn = get_registry_connection(settings)
    try:
        yield conn
    finally:
        conn.close()


# Thin Depends() wrappers around the provider factories — this is what lets
# tests swap in FakeEmbeddingProvider/FakeLLMProvider via
# `app.dependency_overrides` instead of hitting real network calls.
def embedding_provider_dependency(settings: Settings = Depends(get_settings)) -> EmbeddingProvider:
    try:
        return get_embedding_provider(settings)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


def llm_provider_dependency(settings: Settings = Depends(get_settings)) -> LLMProvider:
    try:
        return get_llm_provider(settings)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


async def _to_async_iter(sync_gen: Iterator[dict]) -> AsyncIterator[dict]:
    """Runs each `next()` call (which may block on embedding/LLM network
    I/O) in the default executor, so the SSE stream doesn't block the event
    loop while still driving a plain sync generator underneath."""
    loop = asyncio.get_event_loop()
    it = iter(sync_gen)
    while True:
        item = await loop.run_in_executor(None, next, it, _SENTINEL)
        if item is _SENTINEL:
            break
        yield cast(dict, item)


@router.post("/query")
def query(
    body: QueryRequest,
    settings: Settings = Depends(get_settings),
    registry_conn: sqlite3.Connection = Depends(registry_connection_dependency),
    embedding_provider: EmbeddingProvider = Depends(embedding_provider_dependency),
    llm_provider: LLMProvider = Depends(llm_provider_dependency),
) -> EventSourceResponse:
    row = registry_conn.execute(
        "SELECT status, source_type, display_name, commit_sha FROM repos WHERE id = ?",
        (body.repo_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "repo not found")
    if row["status"] != "ready":
        raise HTTPException(409, f"repo is not ready (status={row['status']})")

    # display_name is already "owner/repo" for github source_type — set
    # that way at clone time (ingest/source.py::_clone_github) — so there's
    # no need to re-parse the source URL here just to build a permalink.
    repo_context = RepoContext(
        source_type=row["source_type"],
        owner_repo=row["display_name"] if row["source_type"] == "github" else None,
        commit_sha=row["commit_sha"],
    )

    try:
        repo_conn = get_repo_connection(body.repo_id, embedding_provider.dim, settings)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc

    def event_stream() -> Iterator[dict]:
        try:
            yield from generate_answer_events(
                repo_conn,
                settings,
                embedding_provider,
                llm_provider,
                repo_context,
                question=body.question,
            )
        finally:
            repo_conn.close()

    return EventSourceResponse(_to_async_iter(event_stream()))
