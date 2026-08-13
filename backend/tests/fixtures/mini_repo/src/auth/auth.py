"""Password hashing helpers.

Target for the natural-language retrieval test "how do we hash passwords"
(SPEC.md Phase 1 acceptance criteria) — this file should rank highly even
though the query shares no exact identifier with `hash_password`.
"""

from __future__ import annotations

import hmac
import os

from src.auth.crypto import derive_key

_ITERATIONS = 200_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256, returning `salt_hex:hash_hex`."""
    salt = salt or os.urandom(16)
    digest = derive_key(password, salt, _ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a plaintext password against a `hash_password` output, in
    constant time."""
    salt_hex, digest_hex = stored.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    candidate = derive_key(password, salt, _ITERATIONS)
    return hmac.compare_digest(candidate.hex(), digest_hex)
