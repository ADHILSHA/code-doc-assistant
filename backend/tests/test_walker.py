from __future__ import annotations

from pathlib import Path

from app.ingest.walker import detect_language, walk_repo


def test_detect_language():
    assert detect_language("src/foo.py") == "python"
    assert detect_language("src/foo.ts") == "typescript"
    assert detect_language("src/foo.tsx") == "tsx"
    assert detect_language("README") is None


def test_walk_repo_kept_and_skipped(mini_repo_path: Path):
    kept, skipped = walk_repo(mini_repo_path)
    kept_paths = {f.path for f in kept}
    skipped_paths = {s.path: s.reason for s in skipped}

    # Should be indexed.
    for expected in (
        "README.md",
        "src/users/service.py",
        "src/users/models.py",
        "src/auth/auth.py",
        "src/big_handler.py",
        "web/userClient.ts",
        "tests/test_service.py",
    ):
        assert expected in kept_paths, f"expected {expected} to be kept"

    # Should never even be walked (directory pruned) — not in kept OR skipped.
    assert not any(p.startswith("node_modules/") for p in kept_paths)
    assert not any(p.startswith("node_modules/") for p in skipped_paths)

    # Should be walked but filtered out, with the right reason.
    assert skipped_paths["ignored.log"] == "gitignored"
    assert skipped_paths["package-lock.json"] == "lockfile"
    assert skipped_paths["app.min.js"] == "minified"
    assert skipped_paths["src/generated/schema_pb2.py"] == "generated"
    assert skipped_paths["logo.png"] == "binary_ext"
    assert skipped_paths["src/legacy_dump.dat2"] == "non_utf8"


def test_walk_repo_marks_test_files(mini_repo_path: Path):
    kept, _ = walk_repo(mini_repo_path)
    by_path = {f.path: f for f in kept}
    assert by_path["tests/test_service.py"].is_test
    assert not by_path["src/users/service.py"].is_test


def test_walk_repo_content_hash_stable(mini_repo_path: Path):
    kept_a, _ = walk_repo(mini_repo_path)
    kept_b, _ = walk_repo(mini_repo_path)
    hashes_a = {f.path: f.content_hash for f in kept_a}
    hashes_b = {f.path: f.content_hash for f in kept_b}
    assert hashes_a == hashes_b
