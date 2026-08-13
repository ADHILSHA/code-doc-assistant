"""GET /repos/{id}/file — SPEC.md §6 Phase 1 task 5 (backs CodeViewer).

Path traversal attempts are explicitly called out as a Phase 3 acceptance
criterion for the agent's read_file tool, but this endpoint is the first
place the app reads an arbitrary user-supplied path from, so it's tested
here now rather than left until Phase 3 catches up.
"""

from __future__ import annotations

from pathlib import Path

from .test_api import _make_client, _wait_for_job


def _index_mini_repo(mini_repo_path: Path, tmp_path: Path):
    # _make_client defaults to allow_local_repos=True — see test_api.py.
    client, _settings = _make_client(tmp_path)
    resp = client.post("/api/repos", json={"source": str(mini_repo_path)})
    body = resp.json()
    _wait_for_job(client, body["job_id"])
    return client, body["repo_id"]


def test_get_file_slice_happy_path(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)

    resp = client.get(f"/api/repos/{repo_id}/file", params={"path": "src/auth/auth.py", "start": 18, "end": 22})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "src/auth/auth.py"
    assert body["language"] == "python"
    assert body["start_line"] == 18
    assert body["end_line"] == 22
    assert len(body["lines"]) == 5
    assert "def hash_password" in body["lines"][0]


def test_get_file_slice_defaults_to_whole_file(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)

    resp = client.get(f"/api/repos/{repo_id}/file", params={"path": "src/auth/auth.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["start_line"] == 1
    assert len(body["lines"]) == body["end_line"]


def test_get_file_slice_clamps_end_beyond_file_length(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)

    resp = client.get(f"/api/repos/{repo_id}/file", params={"path": "src/auth/auth.py", "start": 1, "end": 999999})
    assert resp.status_code == 200
    body = resp.json()
    assert body["end_line"] < 999999


def test_get_file_slice_nonexistent_file_404(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)
    resp = client.get(f"/api/repos/{repo_id}/file", params={"path": "src/does/not/exist.py"})
    assert resp.status_code == 404


def test_get_file_slice_unknown_repo_404(tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.get("/api/repos/does-not-exist/file", params={"path": "a.py"})
    assert resp.status_code == 404


def test_get_file_slice_rejects_path_traversal(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)

    for evil_path in ["../../etc/passwd", "../../../etc/passwd", "src/../../../etc/passwd"]:
        resp = client.get(f"/api/repos/{repo_id}/file", params={"path": evil_path})
        assert resp.status_code == 400, f"{evil_path!r} should have been rejected, got {resp.status_code}"


def test_get_file_slice_rejects_absolute_path(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)
    resp = client.get(f"/api/repos/{repo_id}/file", params={"path": "/etc/passwd"})
    assert resp.status_code == 400


# --- GET /repos/{id}/endpoints, GET /repos/{id}/dependencies (SPEC.md §6 Phase 2 task 5) ---


def test_get_endpoints_matches_mini_repo_ground_truth(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)

    resp = client.get(f"/api/repos/{repo_id}/endpoints")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 9  # 3 fastapi + 3 flask + 3 express, per the fixtures' ground truth
    frameworks = {e["framework"] for e in body}
    assert frameworks == {"fastapi", "flask", "express"}
    health = next(e for e in body if e["route"] == "/health" and e["framework"] == "express")
    assert health["method"] == "GET"
    assert health["auth_hint"] is None


def test_get_dependencies_matches_mini_repo_manifests_exactly(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path)

    resp = client.get(f"/api/repos/{repo_id}/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 9  # 4 npm (package.json) + 5 pypi (pyproject.toml)
    by_name = {d["name"]: d for d in body}
    assert by_name["express"]["version_spec"] == "^4.19.2"
    assert by_name["fastapi"]["ecosystem"] == "pypi"


def test_get_endpoints_unknown_repo_404(tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.get("/api/repos/does-not-exist/endpoints")
    assert resp.status_code == 404


def test_get_dependencies_unknown_repo_404(tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.get("/api/repos/does-not-exist/dependencies")
    assert resp.status_code == 404


def test_get_endpoints_repo_not_ready_409(tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    # A local path that doesn't exist keeps the job (and repo) in a
    # non-ready state instead of ever reaching "ready".
    resp = client.post("/api/repos", json={"source": str(tmp_path / "does-not-exist")})
    repo_id = resp.json()["repo_id"]
    resp = client.get(f"/api/repos/{repo_id}/endpoints")
    assert resp.status_code in (404, 409)
