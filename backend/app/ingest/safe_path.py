"""Shared path-jailing: resolve a user-supplied relative path against a
repo root, rejecting any escape (SPEC.md §7.5: "Path-jail every file read
to the repo root; resolve symlinks and reject escapes"). Used by
api/browse.py's file-serving endpoint; Phase 3's agent `read_file` tool
reuses this rather than reimplementing path-jailing.
"""

from __future__ import annotations

from pathlib import Path


class PathTraversalError(Exception):
    pass


def safe_join(repo_root: Path, relative_path: str) -> Path:
    """Resolves `relative_path` against `repo_root` (symlinks included) and
    raises PathTraversalError if the result escapes `repo_root`.

    An absolute `relative_path` (e.g. `/etc/passwd`) is rejected explicitly
    before joining: pathlib's `/` operator follows os.path.join semantics,
    where joining an absolute path onto anything *discards the left side
    entirely* — `Path("/repo") / "/etc/passwd"` is `/etc/passwd`, not
    `/repo/etc/passwd`. Silently "resolving" that would defeat the jail
    outright, so it's checked before the join, not caught only by the
    after-the-fact `relative_to` bounds check below.
    """
    if Path(relative_path).is_absolute():
        raise PathTraversalError(f"Path must be relative: {relative_path!r}")

    root = repo_root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PathTraversalError(f"Path escapes repo root: {relative_path!r}") from None
    return candidate
