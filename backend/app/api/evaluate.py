"""POST /eval/run (SPEC.md §5, §6 Phase 4 task 2). No business logic here
— validate, delegate to app.eval.run_eval, serialize.

Synchronous, matching SPEC.md §5's literal contract (`POST /api/eval/run
{repo_id, suite} → eval report`, not a job to poll) — unlike POST /repos,
which backgrounds indexing. A large golden set through `explain`-route
questions (each a full agent-loop round trip) can take minutes; a future
revision could background this the same way indexing is backgrounded, but
that's a deviation from the frozen contract this phase didn't make
unilaterally. See DECISIONS.md.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.api.query import embedding_provider_dependency, llm_provider_dependency
from app.api.repos import registry_connection_dependency
from app.config import Settings, get_settings
from app.eval.golden import GoldenSetError
from app.eval.run_eval import run_eval_against_repo
from app.models import EvalRunRequest
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import LLMProvider

router = APIRouter(tags=["eval"])


@router.post("/eval/run")
def run_eval_endpoint(
    body: EvalRunRequest,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(registry_connection_dependency),
    embedding_provider: EmbeddingProvider = Depends(embedding_provider_dependency),
    llm_provider: LLMProvider = Depends(llm_provider_dependency),
) -> dict:
    row = conn.execute("SELECT status FROM repos WHERE id = ?", (body.repo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "repo not found")
    if row["status"] != "ready":
        raise HTTPException(409, f"repo is not ready (status={row['status']})")

    try:
        return run_eval_against_repo(
            settings, body.repo_id, body.suite,
            embedding_provider=embedding_provider, llm_provider=llm_provider,
        )
    except GoldenSetError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
