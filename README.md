# Code Documentation Assistant

Ingests a codebase (public/private GitHub repo, or a local directory for
trusted single-user dev use) and answers natural-language questions about
it, with every factual claim backed by a `path:line` citation that's
**verified to exist** before the answer is returned.

See [SPEC.md](SPEC.md) for the full design and phase plan, and
[DECISIONS.md](DECISIONS.md) for every point the implementation deviated
from it (and why), plus real bugs found via live/smoke testing along the
way.

**Status:** All five phases complete — hybrid retrieval, symbol graph +
agent tool-use, hierarchical summaries, an eval harness, and the
productionization pass (private-repo auth, secret redaction, rate
limiting/timeouts, structured logging, Docker, this README).

## What it does

- Clones/walks a repo, chunks it AST-aware (tree-sitter: Python, JS, TS,
  TSX, Go, Java, Rust, Ruby; naive fixed-size windows as the fallback for
  anything else), and builds:
  - a hybrid **retrieval index** (BM25 full-text + dense vectors, fused
    with Reciprocal Rank Fusion),
  - a **symbol/call/import graph** plus structured `dependencies` and
    `endpoints` tables (parsed straight from manifests/route decorators —
    no LLM involved),
  - **hierarchical summaries** (file → directory → repo) for
    "what does this do" questions that don't need source-level detail.
- Routes each question (dependencies / endpoints / locate / overview /
  explain) to the cheapest strategy that can actually answer it — most
  routes answer straight from SQL; `explain` (and the others' fallback)
  runs a tool-using agent (`semantic_search`, `grep`, `read_file`,
  `get_definition`, `find_references`, ...) over the repo.
- Streams the answer over SSE with a live tool-call trail, then verifies
  every `[path:START-END]` citation against real file/line data before
  the client ever sees it — an invalid citation is stripped, not shown.

## Setup

### Option A: Docker Compose (backend + frontend + persistent volume)

```bash
cp .env.example backend/.env   # fill in at least ANTHROPIC_API_KEY + an embedding key
docker compose up --build
```

- Frontend: http://localhost:8080 (nginx serves the built SPA and reverse-proxies `/api` to the backend container — see `frontend/nginx.conf`)
- Backend (direct, for `curl`/debugging): http://localhost:8000
- Indexed repos, embeddings, and clones persist in the `data` named volume across restarts.
- `ALLOW_LOCAL_REPOS` is forced to `false` in `docker-compose.yml` regardless of `backend/.env` — a "local path" inside the container isn't something a caller outside it can usefully point at (see [Security](#security) below).

### Option B: run locally (no Docker)

**Backend:**

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,embeddings]"   # embeddings: voyageai/openai adapters
cp ../.env.example .env              # then fill in ANTHROPIC_API_KEY and an embedding key
uvicorn app.main:app --reload --port 8000
```

**Python version:** use Homebrew's `python3.12` (or another distro build
with `--enable-loadable-sqlite-extensions`), not a pyenv-built interpreter
— pyenv's default build lacks loadable SQLite extension support, which
`sqlite-vec` requires. See DECISIONS.md for the full story.

Run tests (zero network calls — every provider is faked):

```bash
cd backend && pytest
```

**Frontend:**

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — the dev server proxies `/api` to `http://localhost:8000` (`frontend/vite.config.ts`).

## Environment variables

See [.env.example](.env.example) — copy it to `backend/.env`.

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Synthesis (`claude-sonnet-5`) and summarization (`claude-haiku-4-5`) |
| `EMBEDDING_PROVIDER` | `voyage` (default, code-specialized) \| `openai` |
| `VOYAGE_API_KEY` / `OPENAI_API_KEY` | Only needed if `EMBEDDING_PROVIDER` selects that adapter |
| `DATA_DIR` | Where clones and per-repo SQLite DBs are stored |
| `ALLOW_LOCAL_REPOS` | `false` by default — `POST /api/repos` only accepts GitHub URLs. Set `true` only for trusted single-user local dev (see [Security](#security)) |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key encrypting the stored GitHub PAT at rest (`app/security/credentials.py`). Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Required before `POST /api/auth/github-token` will work; the app runs fine without it for public repos |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | Per-client-IP limit on the expensive endpoints (`/api/query`, `/api/eval/run`, `/api/repos*`) — default 30, `0` disables it |
| `REQUEST_TIMEOUT_SECONDS` / `LONG_REQUEST_TIMEOUT_SECONDS` | Wall-clock request budgets — the "long" one applies to `/api/query` and `/api/eval/run` (defaults 30s / 120s) |
| `MAX_CHUNKS_PER_REPO`, `MAX_REPO_SIZE_MB`, `CLONE_TIMEOUT_SECONDS` | Cost/performance guardrails (SPEC.md §7.4-7.5) |

## Private repos (GitHub PAT)

Click the 🔑 icon in the top-right corner of the UI (or `POST
/api/auth/github-token {"token": "..."}`) to store a GitHub [personal
access token](https://github.com/settings/tokens). It's encrypted at rest
(Fernet, keyed by `CREDENTIAL_ENCRYPTION_KEY`), used only as a git
credential when cloning/fetching, and is **never** logged or returned by
the API — `GET /api/auth/github-token` only reports whether one is
configured. See `app/security/credentials.py` and `app/ingest/source.py`
for exactly how it's threaded into `git clone`/`git fetch` without ever
being written into the clone's `.git/config` (a `-c http.extraheader=...`
override, not a token-embedded URL — see that module's docstring for why
the distinction matters).

## Secret redaction

Before anything is written to the index or sent to an embedding/LLM
provider, `app/ingest/redact.py::redact_secrets` strips known credential
formats (AWS/GitHub/Anthropic/OpenAI/Stripe/Google/Slack keys, PEM private
keys, JWTs) and flags generic high-entropy tokens. It's best-effort by
construction — a heuristic scan of arbitrary text can't catch everything —
but meaningfully reduces the chance a credential accidentally committed
upstream ends up stored or sent anywhere by this tool.

## Architecture

```
GitHub URL / local path (dev only)
        │
        ▼
  ingest (clone via git, walk, filter, redact secrets)
        │
        ▼
  parse (tree-sitter) ──▶ symbols, call/import graph, dependencies, endpoints
        │
        ▼
  chunk → embed → SQLite (relational + FTS5 BM25 + sqlite-vec)
        │
        ▼
  hierarchical summaries (file → directory → repo, background job)
        │
        ▼
  question ─▶ router ─┬─▶ dependencies/endpoints/locate/overview (SQL, no LLM)
                       └─▶ explain: hybrid retrieval → graph expansion →
                           rerank → agent tool-use loop → verified citations
                                              │
                                              ▼
                                   SSE stream to the UI
                                   (status / tool / sources / token /
                                    citations / done / error)
```

One SQLite file per repo (`data/dbs/{repo_id}.db`) plus a small registry
DB tracking repos, background jobs, and the encrypted GitHub credential.
No external services beyond the LLM/embedding APIs — everything else
(retrieval, the symbol graph, rate limiting, redaction) runs in-process.

```
┌──────────────┐      /api (reverse-proxied)      ┌──────────────┐
│   frontend    │ ───────────────────────────────▶ │   backend     │
│ React + nginx │                                   │   FastAPI     │
│  (Docker) or  │ ◀─────────────────────────────── │  + SQLite     │
│  vite (dev)   │        SSE stream (/api/query)   │  (data/ volume)│
└──────────────┘                                   └──────┬───────┘
                                                            │
                                              ┌─────────────┴─────────────┐
                                              ▼                           ▼
                                     Anthropic (synthesis,        Voyage/OpenAI
                                     summarization, routing)      (embeddings)
```

## Eval results

`python -m app.eval.run_eval --repo flask` (or `POST /api/eval/run`)
scores a golden question set (`eval/golden/*.yaml`) against an indexed
repo. Latest real run against `pallets/flask` (`eval/report/flask-latest.md`):

| Metric | Value |
|---|---|
| recall@k | 0.81 |
| citation_validity | 1.00 |
| mean_correctness (0-2, LLM-as-judge) | 1.36 |
| latency p50 | 4.5 ms *(structured routes answer straight from SQL — no LLM call)* |
| latency p95 | 4861 ms *(`explain`-route questions, full agent loop)* |
| total cost | $0.32 |

Per DECISIONS.md: this run's `citation_validity` and `mean_correctness`
are computed only over questions that got a real answer — the account's
Anthropic credit balance ran out partway through the golden set, and each
affected question failed independently with a captured error rather than
crashing the run (the same graceful-degradation contract enforced
everywhere else in this project). Re-run with a funded key for a complete
score across every question.

## Security

- `ALLOW_LOCAL_REPOS` is `false` by default. "Local path" means local to
  wherever the *server* runs, not the caller — enabling it on anything but
  a trusted single-user local setup lets anyone who can reach the API ask
  the server to index arbitrary files it can read (SSH keys, `.env`
  files, ...). The Docker Compose deployment forces it off unconditionally.
- The agent never executes anything from an indexed repo — only `git`
  itself is ever invoked as a subprocess (clone/fetch), and every file
  read (`read_file`, `grep`, `find_files`, ...) is path-jailed to the
  repo's own working tree (`app/ingest/safe_path.py`).
- A stored GitHub PAT is encrypted at rest, never logged, and never
  returned by the API (see [above](#private-repos-github-pat)).
- Secrets are redacted before storage/embedding/generation (see
  [above](#secret-redaction)) — best-effort, not a guarantee.
- Rate limiting (`RATE_LIMIT_REQUESTS_PER_MINUTE`) is in-memory,
  per-process, per-client-IP — sufficient for this single-instance
  deployment (see docker-compose.yml), not for a multi-instance one
  (would need a shared store).
- Every request gets a `request_id` (an incoming `X-Request-ID` header if
  the caller supplies one, else a fresh uuid4), threaded through every
  structured log line for that request via a contextvar
  (`app/logging_setup.py`) and echoed back as a response header. Logs are
  JSON lines to stdout containing only method/path/status/duration — never
  headers or request bodies, so a POSTed credential can never reach a log
  line.

## Known limitations

- **Request timeout is a client-side bound, not a guaranteed server-side
  cancellation.** `asyncio.wait_for`-based timeouts stop the client from
  waiting past the budget, but a synchronous route handler (most of this
  app's) runs on an OS thread Python can't forcibly kill, so the work
  keeps running server-side until it naturally finishes — verified by
  testing, documented in `app/middleware.py`'s docstring, not silently
  assumed to work better than it does.
- **Rate limiting doesn't survive a restart or scale across instances** —
  in-memory counters, single process (see Security above).
- The agent path does not retry-once on an unsupported citation the way
  the flat (Phase 1-2) retrieval path does — re-running a whole multi-turn
  tool-use loop for one retry was judged not worth the added cost/latency.
  An invalid citation is still always stripped, just without a second
  attempt at a fully-cited answer.
- Chunker covers Python/JS/TS/TSX/Go/Java/Rust/Ruby + Markdown; anything
  else (and any file tree-sitter can't cleanly parse) falls back to
  fixed-size windows.
- Secret redaction is a heuristic (known formats + a high-entropy-string
  scan) — best-effort, not a guarantee every secret is caught.
- The frontend ships one (dark) theme rather than a light/dark toggle —
  a deliberate scope call for this phase, logged in DECISIONS.md, not an
  oversight; the app was already dark-only from earlier phases and every
  component would need retrofitting to support a second theme correctly.
- No GitHub OAuth device flow — private-repo auth is PAT-only (a
  deliberate choice, see DECISIONS.md: OAuth requires registering an
  external GitHub OAuth App, which this agent can't do on your behalf).
