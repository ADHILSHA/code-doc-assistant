"""All tunables live here — never inline in call sites (SPEC.md §7.6)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[0]=app, [1]=backend, [2]=project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---
    # Absolute by default (derived from this file's location) so behavior
    # doesn't depend on the working directory the server/tests are launched from.
    data_dir: Path = _PROJECT_ROOT / "data"

    # --- CORS ---
    frontend_origin: str = "http://localhost:5173"

    # --- Security ---
    # "local path" means local to wherever this *server* runs, not to whoever
    # calls the API. Off by default: exposing arbitrary server-side
    # filesystem paths to POST /api/repos is an information-disclosure hole
    # the moment this is anything but a single-user, localhost-only setup.
    # Set true in your own .env for local single-user dev use. Tests set it
    # per-Settings-instance (they need it — it's how the suite avoids network
    # calls) without touching this default.
    allow_local_repos: bool = False

    # --- LLM ---
    anthropic_api_key: str | None = None
    synthesis_model: str = "claude-sonnet-5"
    summarization_model: str = "claude-haiku-4-5-20251001"

    # --- Embeddings ---
    # "fake" is used only by tests (see providers/embeddings.py::FakeEmbeddingProvider).
    # Default is voyage-code-3, a code-specialized model — matches SPEC.md §2.
    embedding_provider: Literal["voyage", "openai", "fake"] = "voyage"
    voyage_api_key: str | None = None
    openai_api_key: str | None = None
    voyage_embedding_model: str = "voyage-code-3"
    openai_embedding_model: str = "text-embedding-3-small"

    # --- Chunking (Phase 0: naive fixed-size windows; replaced in Phase 1) ---
    chunk_size_chars: int = 1500
    chunk_overlap_chars: int = 200

    # --- Retrieval (Phase 0: dense-only top-k; hybrid fusion arrives Phase 1) ---
    top_k_dense: int = 10

    # --- Cost / performance guardrails (SPEC.md §7.4-7.5) ---
    max_chunks_per_repo: int = 50_000
    max_repo_size_mb: int = 500
    clone_timeout_seconds: int = 120
    embedding_batch_size: int = 256

    @property
    def clones_dir(self) -> Path:
        return self.data_dir / "clones"

    @property
    def dbs_dir(self) -> Path:
        return self.data_dir / "dbs"

    @property
    def registry_db_path(self) -> Path:
        return self.dbs_dir / "_registry.db"

    def repo_db_path(self, repo_id: str) -> Path:
        return self.dbs_dir / f"{repo_id}.db"

    def repo_clone_path(self, repo_id: str) -> Path:
        return self.clones_dir / repo_id


@lru_cache
def get_settings() -> Settings:
    return Settings()
