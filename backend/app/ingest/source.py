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
from urllib.parse import urlparse

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


_SSH_GITHUB_RE = re.compile(r"^git@github\.com:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")


def _parse_github_url(source: str) -> tuple[str, str] | None:
    """Returns (owner, repo) if `source` points at a GitHub repo, tolerating
    the messy URLs people actually paste: query strings (`?utm_source=...`),
    fragments (`#readme`), extra path segments (`/tree/main`), a trailing
    `.git`, trailing slashes, or `www.` — anything, as long as the host is
    github.com and the path starts with `owner/repo`. Returns None for
    anything else (treated as a local path).
    """
    source = source.strip()

    ssh_match = _SSH_GITHUB_RE.match(source)
    if ssh_match:
        return ssh_match.group("owner"), ssh_match.group("repo")

    parsed = urlparse(source if "://" in source else f"https://{source}")
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "github.com":
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not owner or not repo:
        return None
    return owner, repo


def classify_source(source: str) -> SourceType:
    return "github" if _parse_github_url(source) is not None else "local"


def resolve_source(source: str, repo_id: str, settings: Settings) -> ResolvedSource:
    source = source.strip()
    parsed = _parse_github_url(source)
    if parsed is not None:
        return _clone_github(*parsed, repo_id=repo_id, settings=settings)
    return _validate_local(source)


def _clone_github(owner: str, name: str, *, repo_id: str, settings: Settings) -> ResolvedSource:
    # Clone from a canonical URL we build ourselves, not the raw pasted
    # input — a query string or `/tree/main` suffix passed straight to
    # `git clone` would confuse or break it.
    url = f"https://github.com/{owner}/{name}.git"
    dest = settings.repo_clone_path(repo_id)

    # SPEC.md §6 Phase 4 task 4: a reindex shouldn't re-download the whole
    # repo for a one-file change. If `dest` already holds a clone of this
    # same URL, refresh it in place (fetch + hard reset) instead of
    # deleting and re-cloning from scratch. Falls back to a full fresh
    # clone on *any* failure — corrupted state, wrong remote, network
    # hiccup — a fresh clone is always correct, just slower.
    if not (dest.is_dir() and _try_fetch_and_reset(dest, url, settings)):
        _fresh_clone(dest, url, settings)

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


def _fresh_clone(dest: Path, url: str, settings: Settings) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            check=True, capture_output=True, text=True, timeout=settings.clone_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(f"Clone of {url} timed out after {settings.clone_timeout_seconds}s") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise SourceError(f"git clone failed: {exc.stderr.strip()}") from exc


def _try_fetch_and_reset(dest: Path, url: str, settings: Settings) -> bool:
    """True if `dest` was successfully refreshed in place (fetch + hard
    reset to the remote's current default branch tip). False for any
    reason — not a git repo, points at a different remote, fetch/reset
    failed — the caller falls back to a full fresh clone either way, so
    this never needs to raise, only report whether it worked.

    Generic over the git URL (not GitHub-specific) on purpose: exercised in
    tests against a local `file://` remote instead of a real github.com URL
    (SPEC.md §7.2 — no network calls in tests), since `git`'s own fetch/reset
    behavior doesn't care whether the origin is `https://github.com/...` or
    `file:///...` — only the transport differs.
    """
    remote = _git_output(dest, ["remote", "get-url", "origin"])
    if remote is None or _normalize_git_url(remote) != _normalize_git_url(url):
        return False  # not a git repo at all, or a clone of something else — don't trust it

    branch = _git_output(dest, ["symbolic-ref", "--short", "HEAD"])
    if branch is None:
        return False  # detached HEAD or some other unusual state — don't guess, fall back to a fresh clone

    try:
        # Explicit branch + FETCH_HEAD (not `origin/HEAD`) — unambiguous
        # regardless of how this clone's remote-tracking refs are
        # configured, rather than relying on `origin/HEAD` having been
        # kept in sync.
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", branch],
            cwd=dest, check=True, capture_output=True, text=True,
            timeout=settings.reindex_fetch_timeout_seconds,
        )
        subprocess.run(
            ["git", "reset", "--hard", "FETCH_HEAD"],
            cwd=dest, check=True, capture_output=True, text=True,
            timeout=settings.reindex_fetch_timeout_seconds,
        )
        # Removes anything untracked left over from the previous checkout
        # (build artifacts, __pycache__, ...) so the working tree matches a
        # fresh clone exactly, not just its tracked files.
        subprocess.run(
            ["git", "clean", "-fdx"],
            cwd=dest, check=True, capture_output=True, text=True,
            timeout=settings.reindex_fetch_timeout_seconds,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _normalize_git_url(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git")


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
