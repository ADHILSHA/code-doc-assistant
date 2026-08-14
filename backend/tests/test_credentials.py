"""app/security/credentials.py (SPEC.md §6 Phase 5 task 2): a GitHub PAT
stored encrypted at rest, never returned in plaintext outside a direct
`get_token` call, never silently falling back to unencrypted storage."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import get_registry_connection
from app.security.credentials import (
    GITHUB_PROVIDER,
    CredentialError,
    delete_token,
    get_token,
    has_token,
    store_token,
)

from .conftest import make_settings


def test_store_without_encryption_key_configured_raises(tmp_path: Path):
    settings = make_settings(tmp_path, credential_encryption_key=None)
    conn = get_registry_connection(settings)
    with pytest.raises(CredentialError):
        store_token(conn, GITHUB_PROVIDER, "ghp_faketoken1234567890", settings)


def test_store_and_get_round_trip(tmp_path: Path):
    settings = make_settings(tmp_path, credential_encryption_key=Fernet.generate_key().decode())
    conn = get_registry_connection(settings)

    assert has_token(conn, GITHUB_PROVIDER) is False
    assert get_token(conn, GITHUB_PROVIDER, settings) is None

    store_token(conn, GITHUB_PROVIDER, "ghp_realtokenvalue1234567890", settings)
    assert has_token(conn, GITHUB_PROVIDER) is True
    assert get_token(conn, GITHUB_PROVIDER, settings) == "ghp_realtokenvalue1234567890"


def test_stored_value_is_not_plaintext(tmp_path: Path):
    settings = make_settings(tmp_path, credential_encryption_key=Fernet.generate_key().decode())
    conn = get_registry_connection(settings)
    token = "ghp_realtokenvalue1234567890"
    store_token(conn, GITHUB_PROVIDER, token, settings)

    row = conn.execute(
        "SELECT token_encrypted FROM credentials WHERE provider = ?", (GITHUB_PROVIDER,)
    ).fetchone()
    assert token.encode() not in bytes(row["token_encrypted"])


def test_store_overwrites_existing_token(tmp_path: Path):
    settings = make_settings(tmp_path, credential_encryption_key=Fernet.generate_key().decode())
    conn = get_registry_connection(settings)
    store_token(conn, GITHUB_PROVIDER, "ghp_first00000000000000000", settings)
    store_token(conn, GITHUB_PROVIDER, "ghp_second0000000000000000", settings)

    assert get_token(conn, GITHUB_PROVIDER, settings) == "ghp_second0000000000000000"
    count = conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"]
    assert count == 1  # upsert, not a duplicate row


def test_get_with_wrong_key_raises_rather_than_returning_none(tmp_path: Path):
    """A wrong/rotated key must not be indistinguishable from "no token
    stored" — that would surface downstream as a confusing clone failure
    instead of the actual, actionable problem."""
    settings = make_settings(tmp_path, credential_encryption_key=Fernet.generate_key().decode())
    conn = get_registry_connection(settings)
    store_token(conn, GITHUB_PROVIDER, "ghp_realtokenvalue1234567890", settings)

    wrong_key_settings = make_settings(
        tmp_path, credential_encryption_key=Fernet.generate_key().decode()
    )
    with pytest.raises(CredentialError):
        get_token(conn, GITHUB_PROVIDER, wrong_key_settings)


def test_delete_is_idempotent(tmp_path: Path):
    settings = make_settings(tmp_path, credential_encryption_key=Fernet.generate_key().decode())
    conn = get_registry_connection(settings)
    store_token(conn, GITHUB_PROVIDER, "ghp_realtokenvalue1234567890", settings)

    assert delete_token(conn, GITHUB_PROVIDER) is True
    assert has_token(conn, GITHUB_PROVIDER) is False
    assert delete_token(conn, GITHUB_PROVIDER) is False  # already gone
