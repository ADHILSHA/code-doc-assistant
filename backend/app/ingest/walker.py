"""File discovery: walk a repo root, apply filters.py, yield DiscoveredFile
records ready for `index/store.py` to persist and chunk.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from app.ingest.filters import evaluate_file, load_gitignore, should_skip_dir
from app.parsing.languages import detect_language

__all__ = ["DiscoveredFile", "SkippedFile", "detect_language", "walk_repo"]


@dataclass(frozen=True)
class DiscoveredFile:
    path: str  # posix-relative to repo root
    language: str | None
    content_hash: str
    size_bytes: int
    loc: int
    is_test: bool
    text: str


@dataclass(frozen=True)
class SkippedFile:
    path: str
    reason: str


def walk_repo(
    repo_root: Path, *, include_migrations: bool = False
) -> tuple[list[DiscoveredFile], list[SkippedFile]]:
    """Walk `repo_root`, returning (kept files, skipped files with reasons).

    Directory pruning happens before descending, so skipped directories
    (node_modules, .git, ...) are never even stat'd.
    """
    gitignore = load_gitignore(repo_root)
    kept: list[DiscoveredFile] = []
    skipped: list[SkippedFile] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames.sort()
        pruned = []
        for d in dirnames:
            abs_dir = Path(dirpath) / d
            rel_dir = abs_dir.relative_to(repo_root).as_posix()
            if should_skip_dir(d, include_migrations=include_migrations):
                continue
            if gitignore is not None and gitignore.match_file(rel_dir + "/"):
                continue
            pruned.append(d)
        dirnames[:] = pruned

        for name in sorted(filenames):
            abs_path = Path(dirpath) / name
            rel = abs_path.relative_to(repo_root).as_posix()
            result = evaluate_file(
                repo_root, abs_path, gitignore=gitignore, include_migrations=include_migrations
            )
            if not result.keep:
                skipped.append(SkippedFile(path=rel, reason=result.reason or "unknown"))
                continue
            assert result.text is not None
            kept.append(
                DiscoveredFile(
                    path=rel,
                    language=detect_language(rel),
                    content_hash=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
                    size_bytes=len(result.text.encode("utf-8")),
                    loc=len(result.text.splitlines()),
                    is_test=result.is_test,
                    text=result.text,
                )
            )

    return kept, skipped
