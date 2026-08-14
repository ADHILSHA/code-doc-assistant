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

    # --- Chunking ---
    # Naive fixed-size windows — the Phase 0 splitter, kept as the fallback
    # for unsupported languages / unparseable files (parsing/chunker.py).
    chunk_size_chars: int = 1500
    chunk_overlap_chars: int = 200
    # AST-aware chunking (Phase 1): functions over this estimated token
    # count get split on statement boundaries, with this many statements
    # of overlap between consecutive splits (SPEC.md §6 Phase 1 task 1).
    chunk_max_tokens: int = 800
    chunk_overlap_statements: int = 1

    # --- Retrieval ---
    top_k_dense: int = 10
    # Hybrid fusion (Phase 1): BM25 + dense, fused with Reciprocal Rank
    # Fusion (SPEC.md §6 Phase 1 task 3).
    top_k_lexical: int = 10
    rrf_k: int = 60
    hybrid_top_n: int = 30

    # --- Graph expansion (Phase 3 task 1) ---
    expand_max_hops: int = 2
    # Same rough chars/4 approximation `index/store.py` already uses for
    # `chunks.token_count` — good enough for a budget cutoff, not an exact count.
    expand_token_budget: int = 4000

    # --- Reranking (Phase 3 task 2) ---
    rerank_keep_n: int = 12

    # --- Agent loop (Phase 3 task 3) ---
    agent_max_iterations: int = 15
    agent_max_context_tokens: int = 60_000
    agent_max_wall_seconds: int = 40
    agent_read_file_max_lines: int = 400
    agent_grep_max_results: int = 50

    # --- Hierarchical summaries (Phase 3 task 4) ---
    summary_min_loc: int = 20
    # File-level summaries are independent of each other (unlike
    # directory/repo-level ones, which need their children's summaries
    # first) — run this many summarization calls concurrently instead of
    # one-at-a-time, so a repo with dozens/hundreds of files doesn't spend
    # minutes with the job stuck at "summarizing" and no visible progress.
    summary_concurrency: int = 6
    # Hard cap on how many *uncached* files get summarized in one run — a
    # cost/latency guardrail (SPEC.md §7.4) for very large repos, same
    # spirit as MAX_CHUNKS_PER_REPO below. Files beyond this cap are simply
    # not summarized this run (they'll be picked up on a later reindex);
    # indexing itself is never blocked on it.
    summary_max_files_per_run: int = 300

    # --- Session memory (Phase 3 task 5) ---
    session_history_turns: int = 3

    # --- Evaluation (Phase 4 tasks 1-3) ---
    # Approximate, for the `cost_per_query` metric only — not billing-grade.
    # Per-model pricing changes over time; these are order-of-magnitude
    # defaults for the synthesis model, overridable per deployment.
    cost_per_1k_input_tokens_usd: float = 0.003
    cost_per_1k_output_tokens_usd: float = 0.015
    # Independent fields (not both derived from one `eval_dir`), on
    # purpose: `golden_dir` should always point at the real, committed
    # question sets (so a test run genuinely exercises the authored
    # golden set), while `eval_report_dir` needs to be redirectable to a
    # throwaway location in tests — writing real report files into the
    # repo on every test run would be its own kind of test pollution.
    golden_dir: Path = _PROJECT_ROOT / "eval" / "golden"
    eval_report_dir: Path = _PROJECT_ROOT / "eval" / "report"

    # --- Incremental reindex (Phase 4 task 4) ---
    # Cap on how many changed/added files a reindex will fetch+reset for
    # before falling back to a full re-clone — protects against a shallow
    # clone's history not reaching far enough back for `git fetch` alone to
    # resolve (SPEC.md §7.4 cost/performance guardrail spirit).
    reindex_fetch_timeout_seconds: int = 60

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
