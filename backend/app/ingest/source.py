"""Resolve a repo source (GitHub URL or local path) into a working directory
+ commit SHA. SPEC.md §7.5: shallow-clone with a timeout, cap total repo
size, and never execute anything from the cloned repo — only `git` itself
is ever invoked here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.models import SourceType


class SourceError(Exception):
    pass


@dataclass(frozen=True)
class ResolvedSource:
    source_type: SourceType
    local_path: Path
    commit_sha: str | None
    default_branch: str | None
    display_name: str


_GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


def classify_source(source: str) -> SourceType:
    return "github" if _GITHUB_URL_RE.match(source.strip()) else "local"


def resolve_source(source: str, repo_id: str, settings: Settings) -> ResolvedSource:
    source = source.strip()
    if classify_source(source) == "github":
        return _clone_github(source, repo_id, settings)
    return _validate_local(source)


def _clone_github(url: str, repo_id: str, settings: Settings) -> ResolvedSource:
    match = _GITHUB_URL_RE.match(url)
    assert match is not None
    owner, name = match.group(1), match.group(2)

    dest = settings.repo_clone_path(repo_id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.clone_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(f"Clone of {url} timed out after {settings.clone_timeout_seconds}s") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(f"git clone failed: {exc.stderr.strip()}") from exc

    size_mb = _dir_size_bytes(dest) / (1024 * 1024)
    if size_mb > settings.max_repo_size_mb:
        shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(
            f"Repo is {size_mb:.0f}MB, over the {settings.max_repo_size_mb}MB cap"
        )

    commit_sha = _git_output(dest, ["rev-parse", "HEAD"])
    default_branch = _git_output(dest, ["symbolic-ref", "--short", "HEAD"])

    return ResolvedSource(
        source_type="github",
        local_path=dest,
        commit_sha=commit_sha,
        default_branch=default_branch,
        display_name=f"{owner}/{name}",
    )


def _validate_local(path_str: str) -> ResolvedSource:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        raise SourceError(f"Local path does not exist: {path}")
    if not path.is_dir():
        raise SourceError(f"Local path is not a directory: {path}")

    commit_sha = _git_output(path, ["rev-parse", "HEAD"])
    default_branch = _git_output(path, ["symbolic-ref", "--short", "HEAD"])

    return ResolvedSource(
        source_type="local",
        local_path=path,
        commit_sha=commit_sha,
        default_branch=default_branch,
        display_name=path.name,
    )


def _git_output(cwd: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=True
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return result.stdout.strip() or None


def _dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
