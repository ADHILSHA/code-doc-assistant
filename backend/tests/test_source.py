from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.ingest.source import (
    SourceError,
    _auth_config_args,
    _clone_github,
    _fresh_clone,
    _scrub,
    _try_fetch_and_reset,
    classify_source,
    resolve_source,
)

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


# --- efficient re-fetch (SPEC.md §6 Phase 4 task 4): real git mechanics
# against a local `file://` remote, not mocked — `git fetch`/`reset --hard`
# behave identically regardless of transport, and `file://` makes no
# network call (SPEC.md §7.2).


def _make_local_remote(tmp_path: Path) -> tuple[str, Path]:
    """A bare repo (the 'remote') plus a working clone to push commits
    from. Returns (file:// url, work_dir)."""
    remote_dir = tmp_path / "remote.git"
    work_dir = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote_dir)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote_dir), str(work_dir)], check=True, capture_output=True)
    _commit(work_dir, "a.txt", "v1", "initial commit")
    subprocess.run(["git", "-C", str(work_dir), "push", "origin", "main"], check=True, capture_output=True)
    return f"file://{remote_dir}", work_dir


def _commit(work_dir: Path, filename: str, content: str, message: str) -> None:
    (work_dir / filename).write_text(content)
    subprocess.run(["git", "-C", str(work_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-m", message],
        check=True, capture_output=True,
    )


def test_fresh_clone_against_local_remote(tmp_path: Path):
    settings = make_settings(tmp_path)
    url, _work_dir = _make_local_remote(tmp_path)
    dest = tmp_path / "clone"

    _fresh_clone(dest, url, settings)
    assert (dest / "a.txt").read_text() == "v1"


def test_try_fetch_and_reset_updates_existing_clone_to_new_upstream_commit(tmp_path: Path):
    settings = make_settings(tmp_path)
    url, work_dir = _make_local_remote(tmp_path)
    dest = tmp_path / "clone"
    _fresh_clone(dest, url, settings)

    _commit(work_dir, "a.txt", "v2", "second commit")
    (work_dir / "b.txt").write_text("brand new file")
    subprocess.run(["git", "-C", str(work_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-m", "add b.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work_dir), "push", "origin", "main"], check=True, capture_output=True)

    ok = _try_fetch_and_reset(dest, url, settings)
    assert ok is True
    assert (dest / "a.txt").read_text() == "v2"
    assert (dest / "b.txt").read_text() == "brand new file"


def test_try_fetch_and_reset_removes_files_deleted_upstream(tmp_path: Path):
    """`git reset --hard` alone updates tracked files but a file removed
    upstream must actually disappear locally too, not linger."""
    settings = make_settings(tmp_path)
    url, work_dir = _make_local_remote(tmp_path)
    dest = tmp_path / "clone"
    _fresh_clone(dest, url, settings)
    assert (dest / "a.txt").exists()

    (work_dir / "a.txt").unlink()
    subprocess.run(["git", "-C", str(work_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work_dir), "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-m", "remove a.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(work_dir), "push", "origin", "main"], check=True, capture_output=True)

    ok = _try_fetch_and_reset(dest, url, settings)
    assert ok is True
    assert not (dest / "a.txt").exists()


def test_try_fetch_and_reset_returns_false_for_a_different_remote(tmp_path: Path):
    settings = make_settings(tmp_path)
    url, _work_dir = _make_local_remote(tmp_path)
    other_url, _other_work_dir = _make_local_remote(tmp_path / "other")
    dest = tmp_path / "clone"
    _fresh_clone(dest, url, settings)

    assert _try_fetch_and_reset(dest, other_url, settings) is False
    # nothing was touched — still at the original clone's content
    assert (dest / "a.txt").read_text() == "v1"


def test_try_fetch_and_reset_returns_false_for_a_non_git_directory(tmp_path: Path):
    settings = make_settings(tmp_path)
    url, _work_dir = _make_local_remote(tmp_path)
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    assert _try_fetch_and_reset(not_a_repo, url, settings) is False


def test_clone_github_reuses_existing_clone_via_fetch_and_reset(tmp_path: Path, monkeypatch):
    """`_clone_github`'s own branching: when `dest` already looks like a
    valid clone of the same URL, it must take the fetch+reset path, not
    issue a fresh `git clone` at all."""
    settings = make_settings(tmp_path)
    dest = settings.repo_clone_path("repo1")
    dest.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="https://github.com/pallets/flask.git\n", stderr="")
        if cmd[:2] == ["git", "symbolic-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        if cmd[:2] == ["git", "fetch"] or cmd[:2] == ["git", "reset"] or cmd[:2] == ["git", "clean"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = _clone_github("pallets", "flask", repo_id="repo1", settings=settings)

    assert resolved.commit_sha == "deadbeef"
    assert not any(cmd[:2] == ["git", "clone"] for cmd in calls), "should not have done a fresh clone"
    assert any(cmd[:2] == ["git", "fetch"] for cmd in calls)


def test_clone_github_falls_back_to_fresh_clone_when_existing_dest_is_not_a_git_repo(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    dest = settings.repo_clone_path("repo1")
    dest.mkdir(parents=True)  # exists, but has no .git — not a real clone
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "remote", "get-url"]:
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: not a git repository")
        return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = _clone_github("pallets", "flask", repo_id="repo1", settings=settings)
    assert resolved.source_type == "github"
    assert any(cmd[:2] == ["git", "clone"] for cmd in calls)


# --- private-repo auth (SPEC.md §6 Phase 5 task 2): a stored PAT is passed
# as a per-invocation `-c http.extraheader=...` override, never embedded in
# the clone URL — see source.py's module docstring for why.


def test_auth_config_args_empty_for_no_token():
    assert _auth_config_args(None) == []
    assert _auth_config_args("") == []


def test_auth_config_args_shape_for_a_token():
    args = _auth_config_args("ghp_faketoken1234567890")
    assert args[0] == "-c"
    assert args[1].startswith("http.extraheader=AUTHORIZATION: basic ")
    # never the raw token — always base64-encoded "x-access-token:<token>"
    assert "ghp_faketoken1234567890" not in args[1]


def test_scrub_removes_token_from_text():
    assert _scrub("error: bad creds ghp_secret123", "ghp_secret123") == "error: bad creds ***"
    assert _scrub("no token here", None) == "no token here"


def test_clone_github_passes_auth_header_when_token_given(tmp_path: Path, monkeypatch):
    """Command-construction assertion (mocked subprocess, no network call,
    SPEC.md §7.2): a token results in `-c http.extraheader=...` spliced
    right after `git`, and the clone URL itself stays plain (no
    `user:token@` embedding) — see source.py's module docstring."""
    settings = make_settings(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "clone" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = _clone_github(
        "pallets", "flask", repo_id="repo1", settings=settings, github_token="ghp_faketoken1234567890"
    )

    assert resolved.source_type == "github"
    clone_cmd = calls[0]
    assert clone_cmd[0] == "git"
    assert clone_cmd[1] == "-c"
    assert clone_cmd[2].startswith("http.extraheader=AUTHORIZATION: basic ")
    assert "https://github.com/pallets/flask.git" in clone_cmd
    # the token itself never appears anywhere in the command line
    assert not any("ghp_faketoken1234567890" in part for part in clone_cmd)


def test_fresh_clone_with_token_does_not_leak_token_into_git_config(tmp_path: Path):
    """Real git mechanics against a local `file://` remote (no network
    call, SPEC.md §7.2): confirms the token used to authenticate a clone
    never ends up persisted in the new clone's `.git/config`, and
    `git remote get-url` never echoes it back either."""
    settings = make_settings(tmp_path)
    url, _work_dir = _make_local_remote(tmp_path)
    dest = tmp_path / "clone"
    fake_token = "ghp_thisIsAFakeTokenDoNotLeak123456"

    _fresh_clone(dest, url, settings, github_token=fake_token)

    assert (dest / "a.txt").read_text() == "v1"
    git_config = (dest / ".git" / "config").read_text()
    assert fake_token not in git_config

    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=dest, check=True, capture_output=True, text=True
    ).stdout
    assert fake_token not in remote_url


def test_fresh_clone_failure_message_is_scrubbed_of_the_token(tmp_path: Path, monkeypatch):
    """Even though the token is never embedded in a URL git would echo
    back (see above), the failure path defensively scrubs stderr anyway —
    this asserts that guarantee holds if stderr somehow contained it."""
    settings = make_settings(tmp_path)
    fake_token = "ghp_thisIsAFakeTokenDoNotLeak123456"

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            128, cmd, stderr=f"fatal: authentication failed for token {fake_token}"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SourceError) as exc_info:
        _fresh_clone(tmp_path / "clone", "https://github.com/pallets/flask.git", settings, fake_token)

    assert fake_token not in str(exc_info.value)
    assert "***" in str(exc_info.value)
