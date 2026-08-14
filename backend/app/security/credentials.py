"""Encrypted-at-rest storage for third-party credentials (SPEC.md §6 Phase 5
task 2: private-repo auth via a user-pasted GitHub PAT).

Only one provider exists today ("github"), but the table/API are keyed by
provider rather than hardcoded to a single row, so a second provider
wouldn't need a schema change.

Encryption: Fernet (symmetric, authenticated — AES-128-CBC + HMAC under the
hood) keyed off `Settings.credential_encryption_key`. That key must live
outside the DB it protects (in the environment/`.env`, never committed) —
see config.py's comment on that field for how to generate one. Storing or
reading a token without the key configured raises a clear `RuntimeError`
rather than silently falling back to plaintext or silently losing the
token; a misconfigured deployment should fail loudly, not leak secrets.

The token's plaintext must never appear in a log line or an API response
body (SPEC.md §6 Phase 5 task 2) — this module only ever returns it to
direct callers that need it for an actual git operation (see
ingest/source.py); api/auth.py never echoes it back.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings

GITHUB_PROVIDER = "github"


class CredentialError(Exception):
    """Raised for a misconfigured encryption key or corrupt ciphertext —
    never raised for "no token stored", which is a normal, expected state
    represented by `get_token` returning None."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.credential_encryption_key
    if not key:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and set it in backend/.env '
            "before storing or using a credential."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_KEY is set but isn't a valid Fernet key "
            "(must be 32 url-safe base64-encoded bytes, as produced by "
            "Fernet.generate_key())."
        ) from exc


def store_token(conn: sqlite3.Connection, provider: str, token: str, settings: Settings) -> None:
    """Encrypt and upsert `token` for `provider`. Overwrites any existing
    token for the same provider."""
    fernet = _fernet(settings)
    encrypted = fernet.encrypt(token.encode("utf-8"))
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO credentials (provider, token_encrypted, created_at, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET token_encrypted = excluded.token_encrypted, "
        "updated_at = excluded.updated_at",
        (provider, encrypted, now, now),
    )
    conn.commit()


def get_token(conn: sqlite3.Connection, provider: str, settings: Settings) -> str | None:
    """Return the decrypted token for `provider`, or None if none is stored.

    Raises `CredentialError` if a token *is* stored but the configured key
    can't decrypt it (key rotated/lost, or the encryption key is unset) —
    silently returning None there would be indistinguishable from "no
    token stored" and would surface as a confusing clone failure instead
    of the actual, actionable problem.
    """
    row = conn.execute(
        "SELECT token_encrypted FROM credentials WHERE provider = ?", (provider,)
    ).fetchone()
    if row is None:
        return None
    fernet = _fernet(settings)
    try:
        return fernet.decrypt(row["token_encrypted"]).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialError(
            f"Stored {provider} credential could not be decrypted with the "
            "configured CREDENTIAL_ENCRYPTION_KEY (wrong or rotated key?). "
            "Re-store the token."
        ) from exc


def delete_token(conn: sqlite3.Connection, provider: str) -> bool:
    """Returns True if a credential existed and was deleted."""
    cur = conn.execute("DELETE FROM credentials WHERE provider = ?", (provider,))
    conn.commit()
    return cur.rowcount > 0


def has_token(conn: sqlite3.Connection, provider: str) -> bool:
    """Existence check that doesn't require the encryption key — used by
    the GET status endpoint, which reports only whether a token is
    configured, never the token itself."""
    row = conn.execute("SELECT 1 FROM credentials WHERE provider = ?", (provider,)).fetchone()
    return row is not None
