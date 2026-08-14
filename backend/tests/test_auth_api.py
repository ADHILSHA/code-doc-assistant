"""POST/GET/DELETE /api/auth/github-token (SPEC.md §6 Phase 5 task 2):
end-to-end through the real ASGI app. The token itself must never appear
in a response body."""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

from .conftest import make_settings

_TOKEN = "ghp_supersecrettoken1234567890abcdefgh"


def _make_client(tmp_path: Path) -> TestClient:
    settings = make_settings(
        tmp_path, credential_encryption_key=Fernet.generate_key().decode()
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_status_is_false_when_nothing_stored(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.get("/api/auth/github-token")
    assert r.status_code == 200
    assert r.json() == {"configured": False}


def test_set_then_status_then_delete(tmp_path: Path):
    client = _make_client(tmp_path)

    r = client.post("/api/auth/github-token", json={"token": _TOKEN})
    assert r.status_code == 200
    assert r.json() == {"configured": True}

    r = client.get("/api/auth/github-token")
    assert r.json() == {"configured": True}

    r = client.delete("/api/auth/github-token")
    assert r.status_code == 204

    r = client.get("/api/auth/github-token")
    assert r.json() == {"configured": False}


def test_token_never_appears_in_any_response_body(tmp_path: Path):
    client = _make_client(tmp_path)
    r1 = client.post("/api/auth/github-token", json={"token": _TOKEN})
    r2 = client.get("/api/auth/github-token")
    assert _TOKEN not in r1.text
    assert _TOKEN not in r2.text


def test_set_without_encryption_key_configured_returns_500_not_500_with_token_leaked(
    tmp_path: Path,
):
    settings = make_settings(tmp_path, credential_encryption_key=None)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)

    r = client.post("/api/auth/github-token", json={"token": _TOKEN})
    assert r.status_code == 500
    assert _TOKEN not in r.text


def test_empty_token_is_rejected(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.post("/api/auth/github-token", json={"token": ""})
    assert r.status_code == 422  # pydantic min_length=1
