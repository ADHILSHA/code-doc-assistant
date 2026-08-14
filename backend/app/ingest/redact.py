"""Secret redaction (SPEC.md §6 Phase 5 task 3 / §7.5): "redact secrets
before storage and before any model call." Two layers, checked in order:

1. Known key-format patterns (AWS, GitHub, OpenAI/Anthropic-style,
   Stripe, Google, Slack, PEM private-key blocks, JWTs) — high
   confidence, essentially no false-positive risk since these formats are
   distinctive by construction.
2. A generic high-entropy-string heuristic over quoted string literals
   20+ characters long, for secrets that don't match a known format
   (a random API key from some other service, for instance). This is
   necessarily imprecise — a heuristic scan of arbitrary text can't be
   perfect — scoped as tightly as practical (token-shaped charset only,
   a real entropy threshold, requires at least one digit to rule out
   ordinary prose) to keep false positives on legitimate code (hashes
   used in tests, base64 fixtures, long identifiers) rare rather than
   zero. Same "best-effort, documented as such" spirit as this project's
   other necessarily-imprecise heuristics (build.gradle/Gemfile parsing,
   the agent's grep Python fallback).

Applied at every point content is about to be stored in the index or sent
to a model — never on the repo's own files on disk (that would corrupt a
clone/local checkout, and isn't what "redact before storage" means).
"""

from __future__ import annotations

import math
import re
from collections import Counter

_REDACTED = "[REDACTED]"

# Distinctive, low-false-positive secret formats.
_KNOWN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),  # AWS STS temporary access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_)
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),  # GitHub fine-grained PAT
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),  # Anthropic API key
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style secret key
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),  # Stripe keys
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),  # Slack tokens
    re.compile(
        r"-----BEGIN(?: RSA| EC| OPENSSH| DSA)? PRIVATE KEY-----[\s\S]+?"
        r"-----END(?: RSA| EC| OPENSSH| DSA)? PRIVATE KEY-----"
    ),  # PEM private-key blocks
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{10,}\b"),  # JWTs
]

# --- generic high-entropy token heuristic ---

# Deliberately NOT anchored to quote characters: an earlier version only
# scanned inside `"..."`/`'...'`/`` `...` `` pairs, which silently missed
# any secret sitting inside a triple-quoted docstring (`"""..."""` opens
# and closes with *three* quote characters, so a single-quote-delimited
# regex matches an empty span between the first two and never sees the
# docstring's actual content at all) — found via a real smoke test against
# a fixture with a fake secret inside a docstring, not by inspection.
# Scanning raw token-shaped runs instead catches that case along with
# every quoted-literal case the old regex covered.
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_.\-]{20,}")
_TOKEN_CHARSET_RE = re.compile(r"^[A-Za-z0-9+/=_.\-]+$")  # base64/hex/token-shaped, not prose
_ENTROPY_THRESHOLD = 4.2  # bits/char — random base64/hex runs ~4.0-6.0, English prose ~3.5-4.0


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _looks_like_secret(candidate: str) -> bool:
    if not _TOKEN_CHARSET_RE.match(candidate):
        return False
    if not any(c.isdigit() for c in candidate):  # pure-letter strings are almost always words/identifiers
        return False
    return _shannon_entropy(candidate) >= _ENTROPY_THRESHOLD


def redact_secrets(text: str) -> str:
    """Best-effort. Not a guarantee every secret is caught (a heuristic
    scan of arbitrary text can't be), but meaningfully reduces the chance
    a credential accidentally committed to a repo ends up stored in the
    index or sent to an embedding/LLM provider."""
    if not text:
        return text

    redacted = text
    for pattern in _KNOWN_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)

    def _maybe_redact_token(match: re.Match[str]) -> str:
        token = match.group(0)
        return _REDACTED if _looks_like_secret(token) else token

    return _TOKEN_RE.sub(_maybe_redact_token, redacted)
