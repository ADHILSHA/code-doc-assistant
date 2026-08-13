"""End-to-end smoke tests through the ASGI app: index a repo, query it,
check the SSE contract. All providers are faked — zero network calls."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.query import embedding_provider_dependency, llm_provider_dependency
from app.config import Settings, get_settings
from app.db import get_registry_connection
from app.main import create_app
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider
from app.util import now_iso

from .conftest import make_settings


def _make_client(tmp_path: Path, *, settings: Settings | None = None) -> tuple[TestClient, Settings]:
    # allow_local_repos=True: these tests index tests/fixtures/mini_repo by
    # local path through the real HTTP API — that's how the suite avoids
    # network calls (SPEC.md §7.2). Production defaults to False; see
    # config.py::allow_local_repos.
    settings = settings or make_settings(tmp_path, allow_local_repos=True)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[embedding_provider_dependency] = lambda: FakeEmbeddingProvider()
    app.dependency_overrides[llm_provider_dependency] = lambda: FakeLLMProvider()
    return TestClient(app), settings


def _wait_for_job(client: TestClient, job_id: str, *, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    job: dict = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not finish in time (last seen: {job})")


def test_end_to_end_index_and_query(mini_repo_path: Path, tmp_path: Path):
    client, _settings = _make_client(tmp_path)

    resp = client.post("/api/repos", json={"source": str(mini_repo_path)})
    assert resp.status_code == 202
    body = resp.json()
    repo_id, job_id = body["repo_id"], body["job_id"]

    job = _wait_for_job(client, job_id)
    assert job["state"] == "succeeded", job

    repo = client.get(f"/api/repos/{repo_id}").json()
    assert repo["status"] == "ready"
    assert repo["stats"]["chunks"] > 0
    assert repo["stats"]["files_skipped"] > 0  # generated/binary/etc. fixtures were filtered

    events: list[str] = []
    with client.stream(
        "POST", "/api/query", json={"repo_id": repo_id, "question": "how do we hash passwords"}
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                events.append(line)

    joined = "\n".join(events)
    assert "event: sources" in joined
    assert "event: token" in joined
    assert "event: citations" in joined
    assert "event: done" in joined
    # The naive retrieval + fake LLM pipeline should surface a real file path
    # somewhere in the source/citation trail (Phase 0 acceptance criterion).
    assert ".py" in joined or ".ts" in joined or ".md" in joined


def test_repos_list_and_delete(mini_repo_path: Path, tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.post("/api/repos", json={"source": str(mini_repo_path)})
    body = resp.json()
    repo_id, job_id = body["repo_id"], body["job_id"]
    _wait_for_job(client, job_id)

    listed = client.get("/api/repos").json()
    assert any(r["id"] == repo_id for r in listed)

    del_resp = client.delete(f"/api/repos/{repo_id}")
    assert del_resp.status_code == 204
    assert client.get(f"/api/repos/{repo_id}").status_code == 404


def test_reindex_is_idempotent(mini_repo_path: Path, tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.post("/api/repos", json={"source": str(mini_repo_path)})
    body = resp.json()
    repo_id, job_id = body["repo_id"], body["job_id"]
    _wait_for_job(client, job_id)
    first_stats = client.get(f"/api/repos/{repo_id}").json()["stats"]

    reindex_resp = client.post(f"/api/repos/{repo_id}/reindex")
    assert reindex_resp.status_code == 202
    second_job_id = reindex_resp.json()["job_id"]
    _wait_for_job(client, second_job_id)

    second_stats = client.get(f"/api/repos/{repo_id}").json()["stats"]
    assert second_stats["chunks"] == first_stats["chunks"]
    assert second_stats["chunks_embedded"] == 0  # nothing changed -> nothing re-embedded


def test_query_unknown_repo_404(tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.post("/api/query", json={"repo_id": "does-not-exist", "question": "hi"})
    assert resp.status_code == 404


def test_query_repo_not_ready_409(tmp_path: Path):
    client, settings = _make_client(tmp_path)

    # Insert a repo row directly, bypassing the indexing job entirely, so its
    # status is deterministically "pending" rather than racing a background
    # task that TestClient may already have run to completion.
    conn = get_registry_connection(settings)
    conn.execute(
        "INSERT INTO repos (id, source, source_type, display_name, local_path, status, "
        "created_at) VALUES ('repo-x', 'local', 'local', 'x', '', 'pending', ?)",
        (now_iso(),),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/query", json={"repo_id": "repo-x", "question": "hi"})
    assert resp.status_code == 409


def test_create_repo_rejects_local_path_by_default(mini_repo_path: Path, tmp_path: Path):
    """POST /api/repos must refuse local paths unless ALLOW_LOCAL_REPOS is
    explicitly enabled — otherwise anyone who can reach the API could ask
    the server to index arbitrary files on its own filesystem."""
    settings = make_settings(tmp_path)
    assert settings.allow_local_repos is False  # confirms the production default
    client, _ = _make_client(tmp_path, settings=settings)

    resp = client.post("/api/repos", json={"source": str(mini_repo_path)})
    assert resp.status_code == 400
    assert "ALLOW_LOCAL_REPOS" in resp.json()["detail"]
    # Rejected before any indexing job is created — confirm no job/repo leaked through.
    assert client.get("/api/repos").json() == []

    # Explicitly enabling it (mirrors setting ALLOW_LOCAL_REPOS=true) restores
    # the previous behavior — request is accepted, not rejected for this reason.
    # (Not exercised against a github.com source here: that would require a
    # real network clone, which tests must never do — SPEC.md §7.2.)
    settings_allowed = make_settings(tmp_path, allow_local_repos=True)
    client_allowed, _ = _make_client(tmp_path, settings=settings_allowed)
    resp = client_allowed.post("/api/repos", json={"source": str(mini_repo_path)})
    assert resp.status_code == 202
