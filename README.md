# Code Documentation Assistant

Ingests a codebase (GitHub repo or local directory) and answers natural-language
questions about it, with every factual claim backed by a verified `path:line` citation.

See [SPEC.md](SPEC.md) for the full design and phase plan, and [DECISIONS.md](DECISIONS.md)
for where the implementation deviated from it and why.

**Status:** Phase 1 (retrieval quality) — AST-aware chunking, hybrid (dense + BM25) retrieval,
and verified citations with click-through source viewing.

## Setup

### Backend

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,embeddings]"   # embeddings: voyageai/openai adapters
cp ../.env.example .env   # then fill in ANTHROPIC_API_KEY and VOYAGE_API_KEY
uvicorn app.main:app --reload --port 8000
```

**Python version:** use Homebrew's `python3.12` (or another distro build with
`--enable-loadable-sqlite-extensions`), not a pyenv-built interpreter — pyenv's
default build lacks loadable SQLite extension support, which `sqlite-vec`
requires. See DECISIONS.md for the full story.

Run tests (no network calls, fake providers only):

```bash
cd backend
pytest
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The dev server proxies `/api` to `http://localhost:8000`.

## Environment variables

See [.env.example](.env.example). Copy it to `backend/.env`.

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Synthesis (`claude-sonnet-5`) and summarization (`claude-haiku-4-5`) |
| `EMBEDDING_PROVIDER` | `voyage` (default, code-specialized) \| `openai` |
| `VOYAGE_API_KEY` / `OPENAI_API_KEY` | Only needed if `EMBEDDING_PROVIDER` selects that adapter — `VOYAGE_API_KEY` is required with the default provider |
| `DATA_DIR` | Where clones and per-repo SQLite DBs are stored |
| `ALLOW_LOCAL_REPOS` | `false` by default — `POST /api/repos` only accepts GitHub URLs. Set `true` to also index local filesystem paths (safe for this single-user local setup; **do not** enable on a shared/hosted deployment — see DECISIONS.md) |

## Architecture

```
GitHub URL / local path
        │
        ▼
  ingest (clone, walk, filter)
        │
        ▼
  chunk → embed → SQLite (relational + FTS5 + sqlite-vec)
        │
        ▼
  question ─▶ retrieval ─▶ LLM synthesis ─▶ answer + verified citations
                                              (SSE stream to the UI)
```

One SQLite file per repo (`data/dbs/{repo_id}.db`) plus a small registry DB
tracking repos and background jobs. No external services beyond the LLM/embedding APIs.

## Known limitations (Phase 1)

- No symbol graph, dependency/endpoint extraction, or query routing yet — Phase 2.
- No agent/tool-use loop for multi-hop questions yet — Phase 3.
- No eval harness yet — Phase 4.
- Answers are buffered (not truly token-streamed) so citations can be verified before
  display — see DECISIONS.md.
- Chunker covers Python/JS/TS/TSX/Go/Java/Rust/Ruby + Markdown; anything else (and any
  file tree-sitter can't cleanly parse) falls back to fixed-size windows.
- No auth for private repos yet (Phase 5) — see DECISIONS.md for the interim workaround.
