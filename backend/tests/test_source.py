from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.ingest.source import SourceError, _clone_github, classify_source, resolve_source

from .conftest import make_settings


def test_classify_source():
    assert classify_source("https://github.com/pallets/flask") == "github"
    assert classify_source("https://github.com/pallets/flask.git") == "github"
    assert classify_source("git@github.com:pallets/flask.git") == "github"
    assert classify_source("/Users/me/code/flask") == "local"
    assert classify_source("./relative/path") == "local"


def test_classify_source_tolerates_messy_pasted_urls():
    """Regression test: a URL copied from a chat UI often carries tracking
    params or a fragment, and github.com URLs for a specific file/branch
    carry extra path segments. All of these must still classify as
    'github', not silently fall through to being treated as a local path."""
    assert classify_source("https://github.com/GonzaloHirsch/express-mongo-typescript-api?utm_source=chatgpt.com") == "github"
    assert classify_source("https://github.com/pallets/flask#readme") == "github"
    assert classify_source("https://github.com/pallets/flask/") == "github"
    assert classify_source("https://github.com/pallets/flask/tree/main") == "github"
    assert classify_source("https://www.github.com/pallets/flask") == "github"
    assert classify_source("http://github.com/pallets/flask") == "github"
    assert classify_source("https://github.com/pallets") == "local"  # no repo segment
    assert classify_source("https://gitlab.com/pallets/flask") == "local"


def test_resolve_source_local(tmp_path: Path):
    settings = make_settings(tmp_path)
    resolved = resolve_source(str(tmp_path), "repo1", settings)
    assert resolved.source_type == "local"
    assert resolved.local_path == tmp_path.resolve()
    assert resolved.display_name == tmp_path.name


def test_resolve_source_local_missing_path(tmp_path: Path):
    settings = make_settings(tmp_path)
    with pytest.raises(SourceError):
        resolve_source(str(tmp_path / "does-not-exist"), "repo1", settings)


def test_resolve_source_local_not_a_directory(tmp_path: Path):
    settings = make_settings(tmp_path)
    f = tmp_path / "file.txt"
    f.write_text("hi")
    with pytest.raises(SourceError):
        resolve_source(str(f), "repo1", settings)


def test_clone_github_never_makes_a_real_network_call(tmp_path: Path, monkeypatch):
    """Asserts the exact git command construction via a mocked subprocess —
    no network call is made (SPEC.md §7.2)."""
    settings = make_settings(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            dest = Path(cmd[-1])
            dest.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef\n", stderr="")
        if "symbolic-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = _clone_github("pallets", "flask", repo_id="repo1", settings=settings)

    assert resolved.source_type == "github"
    assert resolved.commit_sha == "deadbeef"
    assert resolved.default_branch == "main"
    assert resolved.display_name == "pallets/flask"
    clone_cmd = calls[0]
    assert clone_cmd[:5] == ["git", "clone", "--depth", "1", "https://github.com/pallets/flask.git"]
    # Only `git` itself is ever invoked (clone, then rev-parse, then symbolic-ref) —
    # nothing from the cloned repo's contents is executed.
    assert all(cmd[0] == "git" for cmd in calls)
    assert len(calls) == 3


def test_resolve_source_strips_tracking_params_before_cloning(tmp_path: Path, monkeypatch):
    """Regression test for the exact bug reported: a github.com URL with a
    `?utm_source=...` tracking param must resolve via _clone_github, not
    be misclassified as a local path and fail with a mangled path error."""
    settings = make_settings(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = resolve_source(
        "https://github.com/GonzaloHirsch/express-mongo-typescript-api?utm_source=chatgpt.com",
        "repo1",
        settings,
    )
    assert resolved.source_type == "github"
    assert resolved.display_name == "GonzaloHirsch/express-mongo-typescript-api"
    clone_cmd = calls[0]
    assert clone_cmd[4] == "https://github.com/GonzaloHirsch/express-mongo-typescript-api.git"
