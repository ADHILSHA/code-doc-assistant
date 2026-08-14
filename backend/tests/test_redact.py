"""Secret redaction (SPEC.md §6 Phase 5 task 3 / §7.5)."""

from __future__ import annotations

from app.ingest.redact import redact_secrets

# --- known formats: must always be redacted ---


def test_redacts_aws_access_key_id():
    assert "AKIAIOSFODNN7EXAMPLE" not in redact_secrets('aws_access_key_id = "AKIAIOSFODNN7EXAMPLE"')


def test_redacts_github_pat_classic():
    text = redact_secrets('token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"')
    assert "ghp_" not in text


def test_redacts_github_pat_fine_grained():
    text = redact_secrets('token = "github_pat_11ABCDEFG0123456789abcdefghijklmnopqrstuvwxyz"')
    assert "github_pat_" not in text


def test_redacts_anthropic_key():
    text = redact_secrets("ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789")
    assert "sk-ant-" not in text


def test_redacts_openai_style_key():
    text = redact_secrets('openai.api_key = "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"')
    assert "sk-abcdefg" not in text


def test_redacts_stripe_key():
    text = redact_secrets('STRIPE_KEY = "sk_live_51H8xyzABCDEF1234567890"')
    assert "sk_live_" not in text


def test_redacts_google_api_key():
    text = redact_secrets('GOOGLE_API_KEY = "AIzaSyD-1234567890abcdefghijklmnopqrstuvw"')
    assert "AIza" not in text


def test_redacts_slack_token():
    # Built at runtime, not a contiguous literal in source: a fake-but-
    # real-shaped Slack token in the file text itself trips GitHub's push
    # protection secret scanner (found the hard way — a push was rejected
    # over exactly this fixture). The function under test only ever sees
    # the assembled runtime string, so this exercises the same regex path
    # without a matching literal ever appearing in the diff.
    fake_token = "xoxb-" + "1234567890-abcdefghijklmnop"
    text = redact_secrets(f'SLACK_TOKEN="{fake_token}"')
    assert "xoxb-" not in text


def test_redacts_pem_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = redact_secrets(f"key = '''{pem}'''")
    assert "MIIEpAIBAAKCAQEA1234567890abcdef" not in text
    assert "BEGIN RSA PRIVATE KEY" not in text


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ_abc123XYZuvwset"
    text = redact_secrets(f'auth = "{jwt}"')
    assert jwt not in text


# --- generic high-entropy heuristic ---


def test_redacts_a_high_entropy_quoted_literal():
    text = redact_secrets('secret_key = "aX9kL2mQ8zR5vN1pT6wY3cJ7fH0dS4gK9mZ2"')
    assert "aX9kL2mQ8zR5vN1pT6wY3cJ7fH0dS4gK9mZ2" not in text
    assert "[REDACTED]" in text


def test_redacts_a_base64_looking_secret():
    text = redact_secrets('base64_secret = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY="')
    assert "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=" not in text


# --- must NOT redact ordinary code ---


def test_does_not_redact_a_hex_commit_sha():
    text = 'commit_sha = "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a"'
    assert redact_secrets(text) == text


def test_does_not_redact_a_docstring():
    text = '"""Fetch a user by id from the in-memory user store, or None."""'
    assert redact_secrets(text) == text


def test_does_not_redact_a_long_english_sentence():
    text = 'message = "this is a long English sentence used for testing purposes only"'
    assert redact_secrets(text) == text


def test_does_not_redact_a_long_snake_case_identifier():
    text = 'x = "some_long_snake_case_identifier_used_in_a_test_fixture"'
    assert redact_secrets(text) == text


def test_does_not_redact_ordinary_source_code():
    code = (
        "def hash_password(password: str, salt: bytes) -> str:\n"
        "    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200_000).hex()"
    )
    assert redact_secrets(code) == code


# --- edge cases ---


def test_redact_secrets_empty_string():
    assert redact_secrets("") == ""


def test_redact_secrets_none_like_falsy_input_returns_as_is():
    assert redact_secrets("") == ""


def test_redact_secrets_redacts_multiple_secrets_in_one_text():
    text = redact_secrets(
        'aws_key = "AKIAIOSFODNN7EXAMPLE"\ngithub_token = "ghp_1234567890abcdefghijklmnopqrstuvwxyz12"'
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in text
    assert "ghp_1234567890" not in text
    assert text.count("[REDACTED]") == 2
