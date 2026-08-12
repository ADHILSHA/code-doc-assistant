"""Password hashing helpers.

Target for the natural-language retrieval test "how do we hash passwords"
(SPEC.md Phase 1 acceptance criteria) — this file should rank highly even
though the query shares no exact identifier with `hash_password`.
"""

from __future__ import annotations

import hashlib
import hmac
import os

_ITERATIONS = 200_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256, returning `salt_hex:hash_hex`."""
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a plaintext password against a `hash_password` output, in
    constant time."""
    salt_hex, digest_hex = stored.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return hmac.compare_digest(candidate.hex(), digest_hex)
