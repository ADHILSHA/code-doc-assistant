"""Rate limiting + request timeout (SPEC.md §6 Phase 5 task 4), mounted in
main.py. See logging_setup.py for the structured-logging half of task 4
(the `request_id` trace both of these log against).
"""

from __future__ import annotations

import asyncio
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings
from app.logging_setup import get_logger, request_id_var

logger = get_logger("app.request")

# Only the expensive, LLM/embedding-backed endpoints are rate-limited —
# see config.py::rate_limit_requests_per_minute's comment for why cheap,
# frequently-polled GETs (/api/jobs/{id}) are deliberately excluded.
_RATE_LIMITED_PREFIXES = ("/api/query", "/api/eval/run", "/api/repos")
# Same set gets the longer request timeout — a full agent-loop turn or an
# entire golden-set eval run can legitimately take a while (see
# api/evaluate.py's module docstring); everything else gets the shorter
# default so a genuinely hung request is caught quickly.
_LONG_RUNNING_PREFIXES = ("/api/query", "/api/eval/run")


def _resolve_settings(request: Request) -> Settings:
    """Resolve `Settings` the same way a route handler's `Depends(get_settings)`
    would — honoring `app.dependency_overrides[get_settings]` if the app has
    one registered (that's how every test in this project swaps in a
    throwaway `Settings` instance, see tests/conftest.py::make_settings).

    Deliberately NOT captured once in `__init__` at app-construction time:
    an earlier version took `settings` as a middleware constructor arg,
    which meant `create_app()`'s own `settings = get_settings()` (the real,
    process-wide `@lru_cache`'d instance) got baked in permanently —
    `app.dependency_overrides[get_settings]`, set *after* `create_app()`
    returns, never reached it. Found via a real smoke test (a rate limit
    of 3 configured through `make_settings(...)` never tripped across 6
    rapid requests — because the middleware was silently still using
    whatever the real global settings' default was), not by inspection.
    """
    override = request.app.dependency_overrides.get(get_settings)
    return override() if override is not None else get_settings()


class RequestContextMiddleware:
    """A per-request id (from an incoming `X-Request-ID` header if the
    caller supplied one, else a fresh uuid4) bound to `request_id_var` so
    every structured log line emitted anywhere while handling this request
    carries it — see logging_setup.py. Echoes it back as an `X-Request-ID`
    response header, enforces a wall-clock timeout, and logs one
    structured completion line per request (method/path/status/duration
    only — never headers or the request body, so this can never leak a
    POSTed credential like the GitHub PAT into a log line).

    Implemented as a **raw ASGI middleware** (`__call__(scope, receive,
    send)`), not a `starlette.middleware.base.BaseHTTPMiddleware`
    subclass, on purpose — found necessary by testing, not chosen upfront.
    An earlier `BaseHTTPMiddleware` version wrapped `asyncio.wait_for`
    around `call_next(request)`; that timed out and returned a 504 object
    correctly, but the *client* still waited for the full, un-cancelled
    handler duration (measured: a 3s dummy handler against a 1s timeout
    produced a 504 delivered at ~3s, not ~1s). Cause: `call_next()` doesn't
    run the downstream app inline — `BaseHTTPMiddleware` spawns it as a
    sibling task in its own internal task group and relays messages
    through an in-memory stream; cancelling the coroutine *my* dispatch
    was awaiting only cancelled that relay, not the sibling task, and the
    task group's `__aexit__` still blocked until the sibling finished.
    Calling the inner ASGI app directly here — `asyncio.wait_for(self.app(
    scope, receive, send_wrapper), timeout=...)` — puts the same coroutine
    on the call stack `wait_for` is watching, so cancellation actually
    reaches it: re-measuring with this version, the 504 is delivered at
    the timeout (~0.1s for a 0.5s handler), and the handler's own
    `await asyncio.sleep(...)` is genuinely interrupted, not merely
    orphaned to finish in the background.

    That fix is real but not complete — worth stating plainly rather than
    overclaiming:
    - It only cancels *async* work that's actually awaiting something
      (`await asyncio.sleep(...)`, an async HTTP call, ...). A synchronous
      route handler (most of this app's — FastAPI runs a plain `def`
      handler via `anyio.to_thread.run_sync`) is executing on a real OS
      thread; cancelling the `await` that's waiting on that thread stops
      *waiting* for it, but Python cannot forcibly kill a running thread,
      so the handler keeps executing to completion in the background
      regardless. A genuine "stop the work early" guarantee for synchronous
      handlers would need cooperative cancellation checks threaded through
      the agent loop / eval loop themselves — out of scope here.
    - For a *streaming* response (EventSourceResponse, used by /api/query),
      `send_wrapper` sees `http.response.start` well before the body
      finishes streaming — so the timeout here bounds time-to-first-byte
      for that endpoint specifically, not total stream duration. Total
      duration there is already bounded independently by the agent loop's
      own `agent_max_wall_seconds` budget (see generation/answer.py).

    For a plain non-streaming `async def` endpoint, and for /api/eval/run
    (a synchronous, non-streaming handler — most of a timeout's practical
    value there is still "the client isn't left hanging past the budget",
    per the OS-thread caveat above), this now measurably bounds real
    client wait time, not just the eventual response's status code.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        settings = _resolve_settings(request)
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        ctx_token = request_id_var.set(request_id)
        start = time.monotonic()
        timeout = (
            settings.long_request_timeout_seconds
            if request.url.path.startswith(_LONG_RUNNING_PREFIXES)
            else settings.request_timeout_seconds
        )

        response_started = False
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await asyncio.wait_for(self.app(scope, receive, send_wrapper), timeout=timeout)
            except TimeoutError:
                duration_ms = int((time.monotonic() - start) * 1000)
                logger.warning(
                    "request timed out",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": 504,
                        "duration_ms": duration_ms,
                    },
                )
                if not response_started:
                    # Headers not sent yet — safe to substitute a clean 504.
                    # If they were already sent (timeout landed mid-stream),
                    # there's nothing left to do but let the connection end;
                    # sending a second http.response.start would violate the
                    # ASGI protocol.
                    resp = JSONResponse({"message": "request timed out"}, status_code=504)
                    resp.headers["X-Request-ID"] = request_id
                    await resp(scope, receive, send)
                return

            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
        finally:
            request_id_var.reset(ctx_token)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window (60s), in-memory, per-client-IP limit over the
    expensive endpoints only (`_RATE_LIMITED_PREFIXES`).

    In-memory and per-process is a deliberate scope limit, not an
    oversight: this backend always runs as a single process (see
    docker-compose.yml) — a multi-instance deployment would need a shared
    store (Redis, etc.) instead, out of scope for this project.

    `BaseHTTPMiddleware` is fine here (unlike RequestContextMiddleware
    above) — this middleware never waits on anything; it either
    short-circuits immediately with a 429 or falls through to `call_next`
    unchanged, so the task-group/cancellation caveat that motivated
    rewriting the timeout middleware as raw ASGI doesn't apply.

    No lock around the counter: `dispatch` is a plain `async def` with no
    `await` between reading and writing `_buckets[client_ip]`, so within a
    single asyncio event loop the read-increment-write is never
    interleaved with another request's — the same reasoning that makes
    ordinary (non-async) Python dict mutation safe under the GIL without
    an explicit lock.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._window_seconds = 60
        self._buckets: dict[str, tuple[int, float]] = {}  # client_ip -> (count, window_start)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = _resolve_settings(request)
        limit = settings.rate_limit_requests_per_minute
        if limit <= 0 or not request.url.path.startswith(_RATE_LIMITED_PREFIXES):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        count, window_start = self._buckets.get(client_ip, (0, now))
        if now - window_start >= self._window_seconds:
            count, window_start = 0, now
        count += 1
        self._buckets[client_ip] = (count, window_start)

        if count > limit:
            retry_after = max(1, int(self._window_seconds - (now - window_start)))
            logger.warning(
                "rate limit exceeded",
                extra={"client_ip": client_ip, "path": request.url.path, "limit": limit},
            )
            return JSONResponse(
                {"message": "rate limit exceeded, try again later"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)
