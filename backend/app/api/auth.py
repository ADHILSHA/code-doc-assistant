"""POST/GET/DELETE /api/auth/github-token (SPEC.md §6 Phase 5 task 2):
store, check, and clear the GitHub PAT used to clone/fetch private repos.

The token itself is never returned by GET (only whether one is configured)
and is never logged — see app/security/credentials.py and
ingest/source.py's use of it.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.api.repos import registry_connection_dependency
from app.config import Settings, get_settings
from app.models import GithubTokenRequest, GithubTokenStatus
from app.security.credentials import (
    GITHUB_PROVIDER,
    CredentialError,
    delete_token,
    has_token,
    store_token,
)

router = APIRouter(tags=["auth"])


@router.post("/auth/github-token", response_model=GithubTokenStatus)
def set_github_token(
    body: GithubTokenRequest,
    conn: sqlite3.Connection = Depends(registry_connection_dependency),
    settings: Settings = Depends(get_settings),
) -> GithubTokenStatus:
    try:
        store_token(conn, GITHUB_PROVIDER, body.token, settings)
    except CredentialError as exc:
        raise HTTPException(500, str(exc)) from exc
    return GithubTokenStatus(configured=True)


@router.get("/auth/github-token", response_model=GithubTokenStatus)
def get_github_token_status(
    conn: sqlite3.Connection = Depends(registry_connection_dependency),
) -> GithubTokenStatus:
    return GithubTokenStatus(configured=has_token(conn, GITHUB_PROVIDER))


@router.delete("/auth/github-token", status_code=204)
def clear_github_token(
    conn: sqlite3.Connection = Depends(registry_connection_dependency),
) -> None:
    delete_token(conn, GITHUB_PROVIDER)
