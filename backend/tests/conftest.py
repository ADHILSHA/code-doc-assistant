from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Settings

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MINI_REPO = FIXTURES_DIR / "mini_repo"


@pytest.fixture
def mini_repo_path() -> Path:
    return MINI_REPO


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Construct a `Settings` instance immune to whatever happens to be in
    the developer's local `backend/.env` (`_env_file=None`).

    Every test that constructs `Settings` must go through this, not
    `Settings(...)` directly — pydantic-settings reads `backend/.env`
    unconditionally otherwise, so a test asserting on a "default" value
    would actually be asserting on whatever a developer happened to have in
    their own gitignored `.env` file at the time, not the documented
    class-level default in `config.py`. (Found the hard way: this silently
    passed for a long time because no one's local `.env` disagreed with the
    defaults until `ALLOW_LOCAL_REPOS=true` was added for local dev use.)
    """
    defaults: dict[str, Any] = {"data_dir": tmp_path / "data", "embedding_provider": "fake"}
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Settings pointed at a throwaway data dir, forced onto the fake
    (zero-network) embedding provider. Every test that just needs "some
    valid settings" should use this fixture; tests needing specific
    overrides should call `make_settings(tmp_path, **overrides)` directly."""
    return make_settings(tmp_path)
