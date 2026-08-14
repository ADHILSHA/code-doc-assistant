"""Deliberately contains fake, obviously-fabricated credentials — exists
only to verify secret redaction (SPEC.md §6 Phase 5 task 3) actually
strips known-format secrets and high-entropy strings before they're
stored or sent to a model. None of these are real; tests assert they
never appear in a stored chunk, a symbol's docstring, or an agent tool's
output.
"""

# Example (fake) credentials a developer might have accidentally committed.
_LEGACY_AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
_LEGACY_GITHUB_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"


def notes() -> str:
    """A fake, high-entropy-looking secret lives in this docstring too:
    aX9kL2mQ8zR5vN1pT6wY3cJ7fH0dS4gK9mZ2 should never survive indexing."""
    return "see module docstring"
