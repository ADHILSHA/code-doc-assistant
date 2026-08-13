"""Low-level key-derivation primitive, factored out of auth.py.

Exists so the mini_repo fixture has a genuine >=3-file call chain to trace
(SPEC.md Phase 3 acceptance criterion: "a cross-file trace question
produces an answer citing >=3 files in correct call order") —
tests/test_service.py -> src/users/service.py -> src/auth/auth.py ->
src/auth/crypto.py is a real four-file path through the code, not a chain
fabricated only for the test.
"""

from __future__ import annotations

import hashlib


def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 key derivation — the actual cryptographic
    primitive `auth.py`'s `hash_password`/`verify_password` build on."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
