"""File filtering rules (SPEC.md §7.1) — "gets it right once" in Phase 0.

`evaluate_file` is the single entry point: it does both path/name-based
checks and content-based checks (size, UTF-8, LOC, avg line length), and
hands back the decoded text alongside the verdict so `walker.py` doesn't
have to re-read the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pathspec

SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".nuxt",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
}
# Only skipped unless the caller opts in (SPEC.md: "migrations (optional flag)").
OPTIONAL_SKIP_DIRS = {"migrations"}

LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "Cargo.lock",
    "go.sum",
}

MINIFIED_SUFFIXES = (".min.js", ".min.css")
GENERATED_SUFFIXES = ("_pb2.py", ".pb.go", ".g.dart")
GENERATED_INFIX = ".generated."

# Non-exhaustive: common binary/image/font/archive/compiled types.
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".avif",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".class", ".jar", ".pyc", ".o", ".a",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac",
    ".db", ".sqlite", ".sqlite3",
}

MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024
MAX_LOC = 5000
MAX_AVG_LINE_LENGTH = 200

# "tests" / "__tests__" / "spec" directories, or filenames like test_x.py,
# x_test.py, x.test.ts, x.spec.ts.
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+\.|_test\.[^./]+$|\.(test|spec)\.[^./]+$"
)


@dataclass(frozen=True)
class FilterResult:
    keep: bool
    reason: str | None = None
    is_test: bool = False
    text: str | None = None  # decoded content, populated only when keep=True


def should_skip_dir(name: str, *, include_migrations: bool = False) -> bool:
    if name in SKIP_DIRS:
        return True
    return name in OPTIONAL_SKIP_DIRS and not include_migrations


def is_lockfile(name: str) -> bool:
    return name in LOCKFILE_NAMES


def is_minified(name: str) -> bool:
    return name.endswith(MINIFIED_SUFFIXES)


def is_generated(name: str) -> bool:
    return name.endswith(GENERATED_SUFFIXES) or GENERATED_INFIX in name


def is_binary_ext(name: str) -> bool:
    return Path(name).suffix.lower() in BINARY_EXTENSIONS


def is_test_path(rel_posix_path: str) -> bool:
    return bool(TEST_PATH_RE.search(rel_posix_path))


def load_gitignore(repo_root: Path) -> pathspec.PathSpec | None:
    gi_path = repo_root / ".gitignore"
    if not gi_path.exists():
        return None
    lines = gi_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return pathspec.PathSpec.from_lines("gitignore", lines)


def evaluate_file(
    repo_root: Path,
    abs_path: Path,
    *,
    gitignore: pathspec.PathSpec | None = None,
    include_migrations: bool = False,
) -> FilterResult:
    rel = abs_path.relative_to(repo_root).as_posix()
    name = abs_path.name

    if gitignore is not None and gitignore.match_file(rel):
        return FilterResult(False, "gitignored")
    if is_lockfile(name):
        return FilterResult(False, "lockfile")
    if is_minified(name):
        return FilterResult(False, "minified")
    if is_generated(name):
        return FilterResult(False, "generated")
    if is_binary_ext(name):
        return FilterResult(False, "binary_ext")

    try:
        size = abs_path.stat().st_size
    except OSError:
        return FilterResult(False, "stat_error")
    if size == 0:
        return FilterResult(False, "empty")
    if size > MAX_FILE_SIZE_BYTES:
        return FilterResult(False, "too_large")

    try:
        raw = abs_path.read_bytes()
    except OSError:
        return FilterResult(False, "read_error")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FilterResult(False, "non_utf8")

    lines = text.splitlines()
    loc = len(lines)
    if loc > MAX_LOC:
        return FilterResult(False, "too_many_lines")
    if loc > 0 and (sum(len(line) for line in lines) / loc) > MAX_AVG_LINE_LENGTH:
        return FilterResult(False, "avg_line_too_long")

    return FilterResult(True, is_test=is_test_path(rel), text=text)
