"""generation/answer.py::_user_facing_error_message (SPEC.md §6 Phase 5
task 1, error states).

Regression coverage for a real finding from a live smoke test (not
inspection): an unhandled `anthropic.BadRequestError` from a real
insufficient-credits response reached the frontend's SSE `error` event as
raw SDK/HTTP internals — `"Error code: 400 - {'type': 'error', 'error':
{...}, 'request_id': '...'}"` — rendered verbatim in the chat UI. Fixed by
mapping known provider exception types to a short, actionable message
before they're ever put in an SSE event; the real exception is still
logged in full server-side (see answer.py's `except` block).

Uses real `anthropic` SDK exception classes (already a project dependency,
via httpx fixtures) rather than a hand-rolled stand-in — this maps against
the SDK's *actual* type names/module, so a fake with a merely similar
shape wouldn't actually exercise the same code path.
"""

from __future__ import annotations

import httpx
import pytest
from anthropic import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from app.generation.answer import _user_facing_error_message

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status_code: int, message: str):
    response = httpx.Response(status_code, request=_REQUEST)
    return cls(message, response=response, body=None)


def test_authentication_error_gets_actionable_message():
    exc = _status_error(AuthenticationError, 401, "invalid x-api-key")
    msg = _user_facing_error_message(exc)
    assert "API key is invalid" in msg
    assert "x-api-key" not in msg  # raw SDK detail must not leak through


def test_rate_limit_error_gets_actionable_message():
    exc = _status_error(RateLimitError, 429, "rate limited")
    assert "rate-limiting" in _user_facing_error_message(exc)


def test_insufficient_credit_bad_request_gets_specific_message():
    """The exact real-world case found via smoke testing."""
    exc = _status_error(
        BadRequestError,
        400,
        "Your credit balance is too low to access the Anthropic API. "
        "Please go to Plans & Billing to upgrade or purchase credits.",
    )
    msg = _user_facing_error_message(exc)
    assert "no available credit" in msg
    assert "credit balance is too low" not in msg  # raw provider text must not leak through
    assert "Plans & Billing" not in msg


def test_generic_bad_request_gets_generic_provider_message():
    exc = _status_error(BadRequestError, 400, "some other validation problem")
    msg = _user_facing_error_message(exc)
    assert "rejected the request" in msg


def test_connection_and_timeout_errors_get_actionable_message():
    assert "Could not reach" in _user_facing_error_message(APIConnectionError(request=_REQUEST))
    assert "Could not reach" in _user_facing_error_message(APITimeoutError(request=_REQUEST))


def test_server_side_provider_error_gets_actionable_message():
    exc = _status_error(InternalServerError, 500, "internal error")
    assert "temporarily unavailable" in _user_facing_error_message(exc)


def test_non_anthropic_exception_gets_generic_fallback_and_never_leaks_raw_text():
    exc = RuntimeError("some internal detail: /Users/x/secret_path, sk-ant-abc123")
    msg = _user_facing_error_message(exc)
    assert msg == "Something went wrong while generating the answer. Please try again."
    assert "secret_path" not in msg
    assert "sk-ant" not in msg


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        ValueError("bad value"),
        _status_error(AuthenticationError, 401, "x"),
        _status_error(RateLimitError, 429, "x"),
    ],
)
def test_message_is_always_a_short_plain_sentence(exc):
    msg = _user_facing_error_message(exc)
    assert isinstance(msg, str)
    assert len(msg) < 200
    assert "{" not in msg  # never raw dict/JSON-shaped SDK internals
