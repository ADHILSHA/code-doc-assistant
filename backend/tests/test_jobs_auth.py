"""jobs.py: a stored GitHub PAT (SPEC.md §6 Phase 5 task 2) is fetched from
the registry DB and threaded through to `resolve_source` for github
sources, and left alone for local sources (no credential lookup needed)."""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

from app import jobs
from app.db import get_registry_connection
from app.providers.llm import FakeLLMProvider
from app.security.credentials import GITHUB_PROVIDER, store_token

from .conftest import MINI_REPO, make_settings


def test_run_index_job_passes_stored_token_for_github_source(tmp_path: Path, monkeypatch):
    settings = make_settings(
        tmp_path, allow_local_repos=True, credential_encryption_key=Fernet.generate_key().decode()
    )
    registry_conn = get_registry_connection(settings)
    store_token(registry_conn, GITHUB_PROVIDER, "ghp_faketoken1234567890", settings)
    registry_conn.close()

    captured: dict = {}

    def fake_resolve_source(source, repo_id, settings, github_token=None):
        captured["github_token"] = github_token
        raise jobs.SourceError("stop here — only checking what was passed in")

    monkeypatch.setattr(jobs, "resolve_source", fake_resolve_source)

    repo_id, job_id = jobs.create_repo_and_job("https://github.com/pallets/flask", settings)
    jobs.run_index_job(job_id, repo_id, "https://github.com/pallets/flask", settings)

    assert captured["github_token"] == "ghp_faketoken1234567890"


def test_run_index_job_does_not_look_up_token_for_local_source(tmp_path: Path, monkeypatch):
    settings = make_settings(
        tmp_path, allow_local_repos=True, credential_encryption_key=Fernet.generate_key().decode()
    )
    # Deliberately no token stored — if jobs.py tried to fetch one for a
    # local source, get_stored_token would just return None anyway, but
    # this test's real point is captured below: github_token stays None.
    captured: dict = {}

    def fake_resolve_source(source, repo_id, settings, github_token=None):
        captured["github_token"] = github_token
        raise jobs.SourceError("stop here — only checking what was passed in")

    monkeypatch.setattr(jobs, "resolve_source", fake_resolve_source)

    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings, summarization_llm_provider=FakeLLMProvider())

    assert captured["github_token"] is None
