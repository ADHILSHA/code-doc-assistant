from __future__ import annotations

from pathlib import Path

from app.ingest.filters import (
    evaluate_file,
    is_binary_ext,
    is_generated,
    is_lockfile,
    is_minified,
    is_test_path,
    load_gitignore,
    should_skip_dir,
)


def test_should_skip_dir_known_names():
    for name in ("node_modules", ".git", "vendor", "dist", "__pycache__", ".venv"):
        assert should_skip_dir(name)
    assert not should_skip_dir("src")


def test_should_skip_dir_migrations_is_optional():
    assert should_skip_dir("migrations")
    assert not should_skip_dir("migrations", include_migrations=True)


def test_name_based_predicates():
    assert is_lockfile("package-lock.json")
    assert is_lockfile("go.sum")
    assert not is_lockfile("package.json")

    assert is_minified("app.min.js")
    assert not is_minified("app.js")

    assert is_generated("schema_pb2.py")
    assert is_generated("routes.pb.go")
    assert is_generated("widget.generated.ts")
    assert not is_generated("service.py")

    assert is_binary_ext("logo.png")
    assert is_binary_ext("archive.zip")
    assert not is_binary_ext("service.py")


def test_is_test_path():
    assert is_test_path("tests/test_service.py")
    assert is_test_path("src/foo_test.go")
    assert is_test_path("src/foo.test.ts")
    assert is_test_path("__tests__/x.js")
    assert not is_test_path("src/users/service.py")


def test_evaluate_file_against_mini_repo(mini_repo_path: Path):
    gitignore = load_gitignore(mini_repo_path)

    def ev(rel: str):
        return evaluate_file(mini_repo_path, mini_repo_path / rel, gitignore=gitignore)

    kept = ev("src/users/service.py")
    assert kept.keep
    assert not kept.is_test
    assert kept.text is not None and "get_user_by_id" in kept.text

    test_file = ev("tests/test_service.py")
    assert test_file.keep
    assert test_file.is_test

    assert ev("ignored.log").reason == "gitignored"
    assert ev("package-lock.json").reason == "lockfile"
    assert ev("app.min.js").reason == "minified"
    assert ev("src/generated/schema_pb2.py").reason == "generated"
    assert ev("logo.png").reason == "binary_ext"
    assert ev("src/legacy_dump.dat2").reason == "non_utf8"


def test_evaluate_file_too_large(tmp_path: Path):
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 400_000)  # well over MAX_FILE_SIZE_BYTES
    result = evaluate_file(tmp_path, big)
    assert not result.keep
    assert result.reason == "too_large"


def test_evaluate_file_too_many_lines(tmp_path: Path):
    huge = tmp_path / "huge.py"
    huge.write_text("x = 1\n" * 6000)
    result = evaluate_file(tmp_path, huge)
    assert not result.keep
    assert result.reason == "too_many_lines"


def test_evaluate_file_avg_line_too_long(tmp_path: Path):
    minified_like = tmp_path / "data.js"
    minified_like.write_text("a" * 5000 + "\n")
    result = evaluate_file(tmp_path, minified_like)
    assert not result.keep
    assert result.reason == "avg_line_too_long"
