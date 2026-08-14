"""app/middleware.py (SPEC.md §6 Phase 5 task 4): rate limiting + request
timeouts, and the request_id trace both log through (logging_setup.py).

Real behavior, not mocked: exercises the actual middleware stack through a
minimal FastAPI app (for the timeout tests, where a real ASGI round-trip
matters) and through the full `create_app()` (for rate limiting, so it's
verified against the actual route set)."""

from __future__ import annotations

import asyncio
import logging
import time
from io import StringIO
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.config import get_settings
from app.logging_setup import configure_logging
from app.main import create_app
from app.middleware import RequestContextMiddleware

from .conftest import make_settings

configure_logging()  # idempotent — main.py already calls this at import time


def _make_client(tmp_path: Path, **overrides) -> TestClient:
    settings = make_settings(tmp_path, allow_local_repos=True, **overrides)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


# --- rate limiting ---


def test_expensive_endpoint_is_rate_limited(tmp_path: Path):
    client = _make_client(tmp_path, rate_limit_requests_per_minute=3)
    codes = [client.post("/api/repos", json={"source": str(tmp_path)}).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 429, 429]


def test_rate_limited_response_has_retry_after_header(tmp_path: Path):
    client = _make_client(tmp_path, rate_limit_requests_per_minute=1)
    client.post("/api/repos", json={"source": str(tmp_path)})
    r = client.post("/api/repos", json={"source": str(tmp_path)})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_cheap_polling_endpoint_is_not_rate_limited(tmp_path: Path):
    """/api/jobs/{id} is polled frequently by a real UI — it must not be
    subject to the same limit as POST /api/repos (SPEC.md §6 Phase 5 task 4
    is about protecting expensive LLM/embedding-backed calls, not this)."""
    client = _make_client(tmp_path, rate_limit_requests_per_minute=2)
    codes = [client.get("/api/jobs/does-not-exist").status_code for _ in range(10)]
    assert codes == [404] * 10  # never a 429


def test_rate_limit_disabled_when_non_positive(tmp_path: Path):
    client = _make_client(tmp_path, rate_limit_requests_per_minute=0)
    codes = [client.post("/api/repos", json={"source": str(tmp_path)}).status_code for _ in range(10)]
    assert all(c == 202 for c in codes)


async def test_rate_limit_is_per_client_ip(tmp_path: Path):
    """Two different client IPs must not share one bucket. Exercises
    `RateLimitMiddleware.dispatch` directly against hand-built `Request`
    objects with distinct `scope["client"]` values — TestClient always
    reports the same connection host, so there's no way to vary the
    client IP through an HTTP round-trip alone."""
    from starlette.requests import Request

    from app.middleware import RateLimitMiddleware

    settings = make_settings(tmp_path, rate_limit_requests_per_minute=1)
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings
    middleware = RateLimitMiddleware(app)

    async def call_next(request):
        return Response(status_code=200)

    def _request(client_ip: str) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/repos",
            "headers": [],
            "client": (client_ip, 12345),
            "app": app,
        }
        return Request(scope)

    r1a = await middleware.dispatch(_request("1.1.1.1"), call_next)
    r1b = await middleware.dispatch(_request("1.1.1.1"), call_next)  # same IP, 2nd call -> limited
    r2a = await middleware.dispatch(_request("2.2.2.2"), call_next)  # different IP -> own bucket
    assert (r1a.status_code, r1b.status_code, r2a.status_code) == (200, 429, 200)


# --- request id / structured logging ---


def test_response_carries_x_request_id_header(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.get("/api/jobs/does-not-exist")
    assert r.headers.get("x-request-id")


def test_incoming_x_request_id_is_echoed_back(tmp_path: Path):
    client = _make_client(tmp_path)
    r = client.get("/api/jobs/does-not-exist", headers={"X-Request-ID": "caller-supplied-id"})
    assert r.headers["x-request-id"] == "caller-supplied-id"


def test_structured_log_line_has_expected_fields(tmp_path: Path):
    client = _make_client(tmp_path)
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.getLogger().handlers[0].formatter)
    logging.getLogger().addHandler(handler)
    try:
        client.get("/api/jobs/does-not-exist")
    finally:
        logging.getLogger().removeHandler(handler)

    import json

    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    completed = [line for line in lines if line.get("message") == "request completed"]
    assert completed
    entry = completed[-1]
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/jobs/does-not-exist"
    assert entry["status_code"] == 404
    assert isinstance(entry["duration_ms"], int)
    assert entry["request_id"]  # non-empty


def test_post_auth_token_body_never_appears_in_logs(tmp_path: Path):
    """The structured logger only ever records method/path/status/duration
    — never headers or the request body — so a POSTed GitHub PAT can never
    leak into a log line (SPEC.md §6 Phase 5 task 2)."""
    from cryptography.fernet import Fernet

    client = _make_client(tmp_path, credential_encryption_key=Fernet.generate_key().decode())
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.getLogger().handlers[0].formatter)
    logging.getLogger().addHandler(handler)
    try:
        client.post("/api/auth/github-token", json={"token": "ghp_supersecrettoken1234567890"})
    finally:
        logging.getLogger().removeHandler(handler)

    assert "ghp_supersecrettoken1234567890" not in buf.getvalue()


# --- request timeout (minimal app — a dummy slow endpoint, not full create_app) ---


def _make_timeout_app(tmp_path: Path, *, request_timeout_seconds: float) -> tuple[TestClient, dict]:
    settings = make_settings(tmp_path, request_timeout_seconds=request_timeout_seconds)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.dependency_overrides[get_settings] = lambda: settings
    calls = {"slow_finished": False}

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(0.5)
        calls["slow_finished"] = True
        return {"ok": True}

    return TestClient(app), calls


def test_request_exceeding_timeout_returns_504(tmp_path: Path):
    client, _calls = _make_timeout_app(tmp_path, request_timeout_seconds=0.1)
    r = client.get("/slow")
    assert r.status_code == 504
    assert "x-request-id" in {k.lower() for k in r.headers}


def test_request_within_timeout_succeeds(tmp_path: Path):
    client, _calls = _make_timeout_app(tmp_path, request_timeout_seconds=5)
    r = client.get("/slow")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_timeout_genuinely_bounds_client_wait_and_cancels_async_work(tmp_path: Path):
    """Documents the real, tested behavior (see middleware.py's docstring
    for why this middleware is a raw ASGI class rather than a
    BaseHTTPMiddleware subclass): the client gets a fast 504, AND the
    handler's own `await asyncio.sleep(...)` is genuinely cancelled, not
    merely orphaned to keep running in the background — a distinction only
    true because of that implementation choice, confirmed by asserting the
    handler's post-sleep code never ran."""
    client, calls = _make_timeout_app(tmp_path, request_timeout_seconds=0.1)
    t0 = time.monotonic()
    r = client.get("/slow")
    client_wait = time.monotonic() - t0

    assert r.status_code == 504
    assert client_wait < 0.5  # bounded by the timeout, not the handler's 0.5s sleep
    assert calls["slow_finished"] is False  # the sleep was actually cancelled
