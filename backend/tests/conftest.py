from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MINI_REPO = FIXTURES_DIR / "mini_repo"


@pytest.fixture
def mini_repo_path() -> Path:
    return MINI_REPO


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Settings pointed at a throwaway data dir, forced onto the fake
    (zero-network) embedding provider. Every test that touches config
    should go through this fixture rather than the real `get_settings()`
    singleton, so tests never share state or touch the real data/ dir."""
    return Settings(data_dir=tmp_path / "data", embedding_provider="fake")
