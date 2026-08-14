# mini_repo

A tiny, hand-built fixture repo used by the backend test suite. It exists to
be asserted against precisely (SPEC.md §7.3), not to look like a real
project.

## What's in here on purpose

- `src/users/service.py` — a known Python API (`get_user_by_id`, `create_user`, `UserService`).
- `src/users/models.py` — a file with no imports.
- `src/auth/auth.py` — password hashing (`hash_password`, `verify_password`) for the
  "how do we hash passwords" natural-language retrieval test.
- `src/big_handler.py` — a single ~900-line function, to exercise statement-boundary
  splitting for oversized functions.
- `web/userClient.ts` — a known TypeScript module.
- `tests/test_service.py` — a test file (should be indexed with `is_test=1`).
- `src/auth/legacy_key_notes.py` — fake credentials (AWS key, GitHub token, a
  high-entropy string in a docstring) for the secret-redaction tests. None real.

## What's in here to be filtered out

- `src/generated/schema_pb2.py` — generated file (`_pb2.py` suffix).
- `node_modules/some_pkg/index.js` — inside a skipped directory.
- `package-lock.json` — lockfile.
- `app.min.js` — minified.
- `logo.png` — binary.
- `ignored.log` — excluded via `.gitignore`.
