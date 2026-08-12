# Code Documentation Assistant

Ingests a codebase (GitHub repo or local directory) and answers natural-language
questions about it, with every factual claim backed by a verified `path:line` citation.

See [SPEC.md](SPEC.md) for the full design and phase plan, and [DECISIONS.md](DECISIONS.md)
for where the implementation deviated from it and why.

**Status:** Phase 0 (walking skeleton) — naive chunking + retrieval, end-to-end plumbing only.
Quality is expected to be poor until Phase 1.

## Setup

### Backend

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example .env   # then fill in ANTHROPIC_API_KEY etc.
uvicorn app.main:app --reload --port 8000
```

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
| `EMBEDDING_PROVIDER` | `voyage` \| `openai` \| `fastembed` (local, offline, no key needed) |
| `VOYAGE_API_KEY` / `OPENAI_API_KEY` | Only needed if `EMBEDDING_PROVIDER` selects that adapter |
| `DATA_DIR` | Where clones and per-repo SQLite DBs are stored |

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

## Known limitations (Phase 0)

- Chunking is naive fixed-size windows, not AST-aware — replaced in Phase 1.
- Retrieval is dense-only (no BM25/hybrid fusion yet) — Phase 1.
- Citations are not yet verified against real line ranges — Phase 1.
- No symbol graph, dependency/endpoint extraction, or query routing yet — Phase 2.
- No agent/tool-use loop for multi-hop questions yet — Phase 3.
- No eval harness yet — Phase 4.
