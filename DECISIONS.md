# Decisions log

Deviations from SPEC.md, with rationale. Newest first.

---

## Phase 0

**`uv` unavailable → pip + venv fallback**
The build machine doesn't have `uv` installed. SPEC.md §2 explicitly allows falling back to pip + venv, so `backend/` is set up as a standard venv + `pyproject.toml` (PEP 621, installed with `pip install -e .`). If `uv` becomes available later, `uv sync` should work unmodified against the same `pyproject.toml`.

**Backend runs on the system Python 3.14.6, not pyenv's 3.12.7 (reversed mid-Phase-0)**
Originally pinned to pyenv's 3.12.7, reasoning that a brand-new Python would have worse wheel availability for compiled deps. That turned out to be the wrong axis: pyenv's own Python builds are compiled *without* `--enable-loadable-sqlite-extensions` by default, so `sqlite3.Connection.enable_load_extension` doesn't exist at all under it — a hard failure for `sqlite-vec`, which is load-bearing for this entire architecture (§2: "SQLite as the single store per repo... `sqlite-vec` (vectors)"). The system/Homebrew Python 3.14.6 *does* support loadable extensions (verified directly), and every dependency in `pyproject.toml` installed cleanly under it. `backend/.python-version` was removed since pyenv is no longer in the path for this project; `requires-python = ">=3.11"` in `pyproject.toml` is still the only hard floor. If you set this project up with a pyenv-built interpreter instead, rebuild it with `PYTHON_CONFIGURE_OPTS="--enable-loadable-sqlite-extensions" pyenv install --force <version>` first.

**`chunk_vectors` dimension is parameterized per repo, not hardcoded to `FLOAT[1024]`**
SPEC.md §4 hardcodes the vec0 table to `FLOAT[1024]`, which matches `voyage-code-3` (the production default). But the offline/test embedding path (`fastembed`'s default `BAAI/bge-small-en-v1.5`) is 384-dim, and the OpenAI adapter's default model is 1536-dim. Rather than force every provider to 1024 (padding/truncating vectors, which distorts cosine similarity), `db.py::get_repo_connection` creates `chunk_vectors` with whatever dimension the active `EmbeddingProvider.dim` reports, and refuses to reopen a repo DB with a provider of a different dimension (points you at reindexing instead of silently corrupting vectors).

**`chunks.id` uses `AUTOINCREMENT`, unlike the plain `INTEGER PRIMARY KEY` in SPEC.md §4**
Found via a failing test, not by inspection: a plain SQLite `INTEGER PRIMARY KEY` is a rowid alias and reuses deleted rowids. `index/store.py::index_file` deletes and reinserts a file's chunk rows whenever its content changes, so without `AUTOINCREMENT` a brand-new chunk could be assigned the same id as a chunk from an old version of the file. Current code already deletes the stale `chunk_vectors` row before that id gets reused, so there's no live correctness bug today — but a historical `query_log.chunk_ids_json` entry would silently start pointing at unrelated content after a later reindex, which is a bad failure mode for an audit trail. `AUTOINCREMENT` (via SQLite's `sqlite_sequence` bookkeeping) closes this off entirely for the cost of one keyword.

**Migrations only include what the current phase needs**
`db.py`'s `REPO_MIGRATIONS` v1 covers `files`, `chunks`, and `query_log` — the tables Phase 0 tasks actually populate. `chunks_fts`, `symbols`, `symbol_refs`, `import_edges`, `dependencies`, `endpoints`, and `summaries` from SPEC.md §4 will land as migration v2/v3/... in the phases that introduce them, keeping schema growth traceable to the phase that needed it.

**Phase 0 chunker is intentionally naive**
Per SPEC.md §6 Phase 0 task 4: fixed ~1500-char windows with 200-char overlap, no AST awareness. This is deliberately replaced in Phase 1 by `parsing/chunker.py` (tree-sitter). Not a deviation — flagging so it's not mistaken for the final chunking strategy.

**Frontend renders plaintext answers in Phase 0 — no `react-markdown`/syntax highlighting yet, and those deps aren't installed**
SPEC.md §6 Phase 0 task 9 explicitly scopes the frontend to "chat panel, plaintext answers"; citation chips + `CodeViewer` click-through are named Phase 1 tasks. `react-markdown`, `rehype-highlight`, and `highlight.js` (listed in §2's tech stack for the *eventual* system) were installed and then removed again once it was clear nothing in Phase 0 would use them — installed-but-inert dependencies are a worse starting point than adding them back in Phase 1 when `MessageBubble`/`CodeViewer` actually need them. The `citations`/`sources` SSE events are still consumed and shown (as plain non-interactive text), since dropping visible signal the backend already sends would be a worse UX than the code cost of rendering it plainly.

**`api/browse.py` and `api/evaluate.py` not stubbed in Phase 0**
SPEC.md §3's full repo layout lists these upfront, but they belong to Phase 1 (`GET /file` for the citation click-through) and Phase 4 (eval) respectively. Per the working agreement ("work one phase at a time"), only `api/repos.py` and `api/query.py` — the endpoints Phase 0 tasks actually require — were built, to avoid dead code.

**FastAPI dependency footgun: `Depends(fn)` where `fn`'s own params aren't themselves `Depends(...)`-wrapped**
`db.py` originally exposed `iter_registry_connection(settings: Settings | None = None)` for use as `Depends(iter_registry_connection)`. This silently breaks dependency overrides: because `settings` wasn't itself declared as `Depends(get_settings)`, FastAPI doesn't know to resolve it through the DI system at all — on `api/query.py` it triggered request-body validation errors (FastAPI tried to parse a `Settings` sub-model out of the JSON body), and on `api/repos.py` it silently fell back to the real global `get_settings()`, so tests were writing to their temp DB but reading from the real `data/dbs/_registry.db`. Fixed by deleting the shared helper and defining `registry_connection_dependency(settings: Settings = Depends(get_settings))` locally in each API module (matching the pattern already used for `embedding_provider_dependency`/`llm_provider_dependency` in `query.py`). Lesson encoded as a comment at both call sites.

**No live embedding/LLM keys available in this environment**
No `ANTHROPIC_API_KEY` / `VOYAGE_API_KEY` in the shell. All backend tests use `FakeEmbeddingProvider` / a fake LLM provider and make zero network calls (required by acceptance criteria). Live end-to-end indexing against a real GitHub repo requires the user to supply real keys in `backend/.env`.
