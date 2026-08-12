# Code Documentation Assistant — Build Spec

> This document is the source of truth for building the project. Read it fully before writing code.
> Implementation is **phase by phase**. Do not start a phase until the previous one passes its acceptance criteria.

---

## 1. What we are building

A system that ingests a codebase (public/private GitHub repo or a local directory) and answers natural-language questions about it:

- "How does authentication work in this repo?"
- "Where is rate limiting implemented?"
- "What API endpoints does this service expose?"
- "What are the third-party dependencies and what are they used for?"
- "Trace what happens when a user submits an order."

Every factual claim in an answer must be backed by a `path:line` citation that is **verified to exist** before the answer is returned.

### 1.1 The core design bet

Naive RAG over code (fixed-size chunks → embed → top-k → answer) fails on this task. Three reasons, and the three corresponding countermeasures that define this architecture:

| Failure | Countermeasure |
|---|---|
| "Where is `X`?" needs exact identifier matching; embeddings return semantically similar but wrong code | **Hybrid retrieval**: BM25 + dense vectors fused with RRF |
| "How does X work?" spans many files; chunk-level retrieval returns fragments with no call context | **Symbol graph + expansion**, and an **agent** that can grep and follow references |
| "What are the dependencies / endpoints?" is a structured question, not a similarity question | **Structured extractors** writing to SQL tables; query router answers these with SQL, not retrieval |

Anything that increases the amount of *structure* we know about the code is high value. Anything that just adds more embeddings is low value.

---

## 2. Tech stack

**Backend**
- Python 3.11+
- FastAPI + Uvicorn, Pydantic v2
- SQLite as the single store per repo: relational tables + FTS5 (BM25) + `sqlite-vec` (vectors)
- `tree-sitter` + `tree-sitter-language-pack` for parsing
- `anthropic` SDK for LLM calls
- `pytest` for tests
- Dependency management: `uv` (fall back to pip + venv if unavailable)

**Frontend**
- React 18 + TypeScript + Vite
- Tailwind CSS
- `react-markdown` for answer rendering, `shiki` or `highlight.js` for code
- Server-Sent Events for streaming answers

**Models** (all behind provider interfaces — never call an SDK directly from business logic)
- Embeddings: default to a code-specialized model (`voyage-code-3`); provide an OpenAI adapter and a local `fastembed` adapter for offline dev/tests
- Synthesis LLM: `claude-sonnet-5`
- Summarization LLM (cheap, high volume): `claude-haiku-4-5-20251001`
- Model IDs change — read them from config, never hardcode in call sites

**Deliberately NOT using** (keep the system small and debuggable):
- No LangChain / LlamaIndex. Retrieval logic is the product; do not hide it behind a framework.
- No external vector DB, Redis, or Postgres in phases 0–4. SQLite is sufficient and makes multi-tenant isolation trivial (one DB file per repo).
- No Docker until phase 5.

---

## 3. Repository layout

```
codebase-qa/
├── SPEC.md                       # this file
├── README.md
├── .env.example
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py               # FastAPI app, CORS, router mounting
│   │   ├── config.py             # pydantic-settings; all env vars, model IDs, limits
│   │   ├── db.py                 # connection factory, migrations, sqlite-vec loading
│   │   ├── models.py             # pydantic schemas shared across layers
│   │   ├── api/
│   │   │   ├── repos.py          # POST /repos, GET /repos, jobs, reindex, delete
│   │   │   ├── query.py          # POST /query (SSE stream)
│   │   │   ├── browse.py         # tree, file slice, endpoints, dependencies
│   │   │   └── evaluate.py       # eval trigger + results (phase 4)
│   │   ├── ingest/
│   │   │   ├── source.py         # GitHub clone / local path → (dir, commit_sha)
│   │   │   ├── walker.py         # file discovery
│   │   │   └── filters.py        # ignore rules, binary/generated/size detection
│   │   ├── parsing/
│   │   │   ├── languages.py      # extension → grammar mapping
│   │   │   ├── chunker.py        # AST-aware chunking
│   │   │   ├── symbols.py        # definition extraction
│   │   │   ├── refs.py           # call/reference extraction, import edges
│   │   │   └── extractors/
│   │   │       ├── dependencies.py   # manifest parsers
│   │   │       └── endpoints/        # one module per framework
│   │   ├── index/
│   │   │   ├── store.py          # writes chunks/symbols/etc. to SQLite
│   │   │   ├── vectors.py        # embedding + sqlite-vec ops
│   │   │   └── lexical.py        # FTS5 setup + BM25 queries
│   │   ├── enrich/
│   │   │   └── summarizer.py     # file → dir → repo summaries (phase 3)
│   │   ├── retrieval/
│   │   │   ├── router.py         # question classification
│   │   │   ├── hybrid.py         # dense + lexical + RRF
│   │   │   ├── expand.py         # graph expansion
│   │   │   └── rerank.py
│   │   ├── agent/
│   │   │   ├── tools.py          # grep, read_file, get_definition, ...
│   │   │   ├── loop.py           # tool-use loop with budgets
│   │   │   └── prompts.py
│   │   ├── generation/
│   │   │   ├── answer.py         # context assembly + synthesis
│   │   │   └── citations.py      # parse + VERIFY citations
│   │   ├── providers/
│   │   │   ├── embeddings.py     # EmbeddingProvider protocol + adapters
│   │   │   └── llm.py            # LLMProvider protocol + adapters
│   │   └── jobs.py               # background indexing jobs + progress
│   └── tests/
│       ├── fixtures/mini_repo/   # tiny hand-built repo, committed to git
│       └── test_*.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx
│       ├── api/client.ts
│       ├── components/
│       │   ├── RepoSelector.tsx
│       │   ├── IndexProgress.tsx
│       │   ├── ChatPanel.tsx
│       │   ├── MessageBubble.tsx
│       │   ├── CitationChip.tsx
│       │   ├── CodeViewer.tsx
│       │   ├── EndpointsTable.tsx
│       │   └── DependencyList.tsx
│       └── types.ts
├── data/                         # gitignored: clones/ and dbs/
└── eval/
    ├── golden/*.yaml             # question sets per test repo
    └── report/                   # generated eval reports
```

---

## 4. Data model

One SQLite file per repo: `data/dbs/{repo_id}.db`. A small global `data/dbs/_registry.db` tracks repos and jobs.

```sql
-- _registry.db
CREATE TABLE repos (
  id TEXT PRIMARY KEY,             -- uuid
  source TEXT NOT NULL,            -- github url or local path
  source_type TEXT NOT NULL,       -- 'github' | 'local'
  display_name TEXT NOT NULL,
  local_path TEXT NOT NULL,
  commit_sha TEXT,
  default_branch TEXT,
  status TEXT NOT NULL,            -- 'pending'|'cloning'|'parsing'|'embedding'|'ready'|'failed'
  error TEXT,
  stats_json TEXT,                 -- files, chunks, languages, loc, index duration
  created_at TEXT, indexed_at TEXT
);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY, repo_id TEXT, type TEXT,       -- 'index' | 'reindex'
  state TEXT, stage TEXT, progress REAL,              -- 0..1
  message TEXT, error TEXT, created_at TEXT, updated_at TEXT
);

-- per-repo db
CREATE TABLE files (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, language TEXT,
  content_hash TEXT NOT NULL, size_bytes INTEGER, loc INTEGER,
  is_test INTEGER DEFAULT 0, last_commit_sha TEXT
);

CREATE TABLE chunks (
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id),
  symbol_name TEXT, symbol_kind TEXT,      -- function|method|class|module|markdown_section
  parent_symbol TEXT, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
  header TEXT,        -- injected context: path, imports, class, signature
  content TEXT NOT NULL, token_count INTEGER
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  content, symbol_name, path, content='', tokenize='unicode61 remove_diacritics 0'
);
-- Tokenizer note: identifiers must be searchable both whole and split.
-- Index both the raw text AND a camelCase/snake_case-split copy of symbol_name.

CREATE VIRTUAL TABLE chunk_vectors USING vec0(
  chunk_id INTEGER PRIMARY KEY, embedding FLOAT[1024]
);

CREATE TABLE symbols (
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, name TEXT NOT NULL,
  kind TEXT NOT NULL, signature TEXT, docstring TEXT, parent_symbol TEXT,
  start_line INTEGER, end_line INTEGER, is_exported INTEGER DEFAULT 1
);
CREATE INDEX idx_symbols_name ON symbols(name);

CREATE TABLE symbol_refs (
  id INTEGER PRIMARY KEY, from_file_id INTEGER, from_symbol_id INTEGER,
  target_name TEXT NOT NULL, resolved_symbol_id INTEGER, line INTEGER
);
CREATE INDEX idx_refs_target ON symbol_refs(target_name);

CREATE TABLE import_edges (
  id INTEGER PRIMARY KEY, from_file_id INTEGER NOT NULL,
  to_file_id INTEGER, module_text TEXT NOT NULL, is_external INTEGER, line INTEGER
);

CREATE TABLE dependencies (
  id INTEGER PRIMARY KEY, ecosystem TEXT,      -- npm|pypi|go|cargo|maven|rubygems
  name TEXT NOT NULL, version_spec TEXT,
  kind TEXT,                                    -- runtime|dev|peer|optional
  manifest_path TEXT NOT NULL, used_in_files_json TEXT
);

CREATE TABLE endpoints (
  id INTEGER PRIMARY KEY, method TEXT, route TEXT NOT NULL, framework TEXT,
  handler_symbol TEXT, file_id INTEGER, line INTEGER,
  auth_hint TEXT, params_json TEXT, source TEXT  -- 'ast' | 'openapi' | 'config'
);

CREATE TABLE summaries (
  id INTEGER PRIMARY KEY, scope TEXT,          -- 'file'|'directory'|'repo'
  target_path TEXT NOT NULL, content TEXT NOT NULL,
  source_hash TEXT NOT NULL, model TEXT, created_at TEXT
);

CREATE TABLE query_log (
  id INTEGER PRIMARY KEY, question TEXT, route TEXT, answer TEXT,
  citations_json TEXT, chunk_ids_json TEXT, tool_calls INTEGER,
  latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, created_at TEXT
);
```

---

## 5. API contract

Freeze these signatures in phase 0 so the frontend can be built in parallel.

```
POST   /api/repos                  {source: string}  → 202 {repo_id, job_id}
GET    /api/repos                  → [{id, display_name, status, stats, indexed_at}]
GET    /api/repos/{id}             → repo detail + stats
DELETE /api/repos/{id}             → 204
POST   /api/repos/{id}/reindex     → 202 {job_id}
GET    /api/jobs/{job_id}          → {state, stage, progress, message, error}

POST   /api/query                  {repo_id, question, session_id?}
                                   → text/event-stream

GET    /api/repos/{id}/tree?path=  → directory listing
GET    /api/repos/{id}/file?path=&start=&end=   → {path, language, lines[]}
GET    /api/repos/{id}/endpoints   → [{method, route, handler, path, line, framework}]
GET    /api/repos/{id}/dependencies → [{ecosystem, name, version, kind, manifest}]
GET    /api/repos/{id}/search?q=   → raw hybrid search results (debug/dev tool)

POST   /api/eval/run               {repo_id, suite}  → eval report   [phase 4]
```

**SSE event types for `/api/query`** — the frontend depends on these exact names:

```
event: status     data: {"stage":"routing"|"retrieving"|"expanding"|"tool_call"|"generating","detail":"..."}
event: tool       data: {"name":"grep","input":{...},"result_summary":"12 matches in 5 files"}
event: sources    data: {"chunks":[{"path":"...","start_line":10,"end_line":48,"symbol":"..."}]}
event: token      data: {"text":"..."}
event: citations  data: {"citations":[{"id":1,"path":"...","start_line":10,"end_line":24,"url":"https://github.com/..."}]}
event: done       data: {"query_id":123,"latency_ms":4210,"route":"explain"}
event: error      data: {"message":"..."}
```

---

## 6. Phase plan

Each phase ends with a working, demoable system. **Commit at the end of each phase on its own branch.** Do not begin the next phase until every acceptance box is checked.

---

### Phase 0 — Walking skeleton (naive but end-to-end)

**Goal:** ask a question about a real repo and get an answer with citations. Quality will be poor. That is expected and fine — this phase exists to lock down the interfaces.

Tasks:
1. Scaffold backend (`uv init`, FastAPI, config, SQLite bootstrap with migrations in `db.py`).
2. `ingest/source.py`: shallow-clone a GitHub URL (`git clone --depth 1`) into `data/clones/{repo_id}`, or validate a local path. Capture commit SHA. **Never execute anything from the cloned repo.**
3. `ingest/walker.py` + `filters.py`: walk files, apply the ignore list from §7.1, record into `files`.
4. Naive chunking: split each file into ~1500-char windows with 200-char overlap. Write to `chunks`.
5. `providers/embeddings.py`: define the `EmbeddingProvider` protocol; implement the real adapter + a `FakeEmbeddingProvider` (deterministic hash-based vectors) used by all tests.
6. `index/vectors.py`: batch-embed chunks, store in `chunk_vectors`, cosine top-k query.
7. `generation/answer.py`: top-10 chunks → prompt → Claude → answer. Stream via SSE.
8. Implement `POST /api/repos`, `GET /api/jobs/{id}`, `POST /api/query`. Indexing runs as a background job writing progress to `jobs`.
9. Frontend: Vite + React + Tailwind scaffold. Repo input box, progress bar, chat panel, plaintext answers.

Acceptance criteria:
- [ ] `POST /api/repos` with a small public repo completes and reaches `status='ready'`
- [ ] Indexing progress is visible in the UI and updates at least once per stage
- [ ] A question returns a streamed answer that references at least one real file path
- [ ] `pytest` passes with zero network calls (fake providers everywhere)
- [ ] Re-running index on the same repo is idempotent (no duplicate rows)

---

### Phase 1 — Real chunking, hybrid retrieval, verified citations

**Goal:** the retrieval quality jump. This is the highest-leverage phase.

Tasks:
1. **tree-sitter chunking** (`parsing/chunker.py`):
   - Support at minimum: Python, JavaScript, TypeScript, TSX, Go, Java, Rust, Ruby.
   - One chunk per function / method / class. Module-level code becomes one chunk.
   - Functions over ~800 tokens split on statement boundaries with 1 statement of overlap.
   - Every chunk gets a `header`: `# path/to/file.py | class UserService | imports: fastapi, sqlalchemy` prepended before embedding (stored separately from `content` so it is not shown as code to the user).
   - Markdown/docs get heading-based chunking.
   - Files in unsupported languages fall back to the phase-0 splitter — log which languages fell back.
2. **Lexical index** (`index/lexical.py`): FTS5 over chunk content + symbol names. Index a normalized copy of identifiers where `getUserById` also yields tokens `get user by id`, so both exact and natural-language queries hit.
3. **Hybrid fusion** (`retrieval/hybrid.py`): run dense and BM25 in parallel, fuse with Reciprocal Rank Fusion (`k=60`). Return top 30 before reranking.
4. **Citation verification** (`generation/citations.py`):
   - Prompt requires the model to cite as `[path/to/file.py:120-145]`.
   - Parse all citations, check the path exists in `files` and the line range is within bounds.
   - Invalid citations are stripped and logged as `hallucinated_citation`; if a claim's only citation is invalid, the answer is regenerated once with the failure noted.
   - Emit GitHub permalinks pinned to the indexed commit SHA.
5. Frontend: render citations as clickable chips; clicking opens `CodeViewer` with the exact line range highlighted, fetched from `/api/repos/{id}/file`.

Acceptance criteria:
- [ ] For the fixture repo, chunk boundaries align to function/class boundaries (assert in tests)
- [ ] A query for an exact identifier (`get_user_by_id`) ranks that definition in the top 3
- [ ] A natural-language query ("how do we hash passwords") retrieves the right file
- [ ] 100% of returned citations resolve to real, in-bounds line ranges (assert in tests)
- [ ] Clicking a citation in the UI shows the highlighted source

---

### Phase 2 — Structure: symbols, graph, dependencies, endpoints, routing

**Goal:** structured questions get exact answers from SQL instead of guessed answers from retrieval.

Tasks:
1. **Symbol extraction** (`parsing/symbols.py`): populate `symbols` for every supported language.
2. **References and imports** (`parsing/refs.py`): extract call expressions into `symbol_refs`; resolve `target_name` against `symbols` by name (accept ambiguity — store all candidates or leave unresolved). Extract import statements into `import_edges`, resolving relative imports to `files` where possible and flagging the rest as external.
3. **Dependency extraction**: parse `package.json`, `requirements*.txt`, `pyproject.toml`, `Pipfile`, `go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle`, `Gemfile`. Record ecosystem, name, version spec, and runtime/dev kind.
4. **Endpoint extraction** (`parsing/extractors/endpoints/`) — one module per framework, each returning a common `Endpoint` record:
   - Python: FastAPI decorators, Flask `@app.route`/blueprints, Django `urls.py`
   - JS/TS: Express `app.<verb>` and `router.<verb>`, NestJS decorators, Next.js file-system routes (`pages/api/**`, `app/**/route.ts`)
   - Java: Spring `@RequestMapping`/`@GetMapping`
   - Ruby: `config/routes.rb`
   - Any committed `openapi.yaml`/`swagger.json` → parse directly, mark `source='openapi'`
   - Detect auth hints from decorators/middleware on the handler (`auth_hint`)
5. **Query router** (`retrieval/router.py`): classify each question into one of five routes. Use a cheap LLM call with a strict enum output, backed by regex fast-paths for the obvious cases.

| Route | Trigger | Strategy |
|---|---|---|
| `dependencies` | "what libraries/packages/deps" | SQL on `dependencies` |
| `endpoints` | "what endpoints/routes/API" | SQL on `endpoints` |
| `locate` | "where is X / which file handles X" | symbol lookup + FTS exact + dense, RRF |
| `explain` | "how does X work / trace X" | hybrid retrieval + graph expansion (phase 3 agent) |
| `overview` | "what does this repo do" | repo + directory summaries (phase 3) |

6. Frontend: `EndpointsTable` and `DependencyList` panels, plus a repo overview tab.

Acceptance criteria:
- [ ] On a FastAPI or Express test repo, ≥90% of real endpoints are extracted with correct method, path, and handler location (verify by hand against the repo)
- [ ] Dependency counts match the manifest exactly
- [ ] `get_definition("SomeClass")` returns the correct `file:line`
- [ ] Router assigns the correct route on a 20-question labeled set
- [ ] Structured routes answer in under 1 second and never hallucinate a package or endpoint that isn't in the DB

---

### Phase 3 — Agent, graph expansion, reranking, summaries

**Goal:** correctly answer multi-hop questions like "trace what happens when a user submits an order."

Tasks:
1. **Graph expansion** (`retrieval/expand.py`): after seed retrieval, add (a) definitions of symbols referenced by seed chunks, (b) callers of seed symbols, (c) the file header/imports. Cap expansion at 2 hops and a configured token budget.
2. **Reranking** (`retrieval/rerank.py`): cross-encoder or LLM-based scoring of 30 candidates → keep top 12. Merge adjacent chunks from the same file into one contiguous span.
3. **Agent loop** (`agent/`) with these tools:
   ```
   semantic_search(query, k)            grep(pattern, glob?, max_results)
   find_files(glob)                     read_file(path, start_line, end_line)
   get_definition(symbol)               find_references(symbol)
   list_directory(path)                 get_dependencies()
   list_endpoints()                     get_summary(path)
   ```
   - `grep` runs `ripgrep` against the working tree if available, else a Python fallback.
   - `read_file` is capped at 400 lines per call and is path-jailed to the repo root.
   - Budgets: max 15 iterations, max ~60k context tokens, max 40s wall clock. On exhaustion, answer with what was gathered and say so.
   - Seed the agent with the phase-2 hybrid retrieval results so it never starts blind.
   - Emit an SSE `tool` event for every call so the UI shows the agent's reasoning trail.
4. **Hierarchical summaries** (`enrich/summarizer.py`): file → directory → repo, bottom-up, cheap model, cached by `source_hash`. Embed summaries into the same vector index with `symbol_kind='summary'`. Skip files under 20 LOC.
5. **Session memory**: rewrite follow-up questions against the last 3 turns so pronouns resolve ("how does *it* handle errors?").

Acceptance criteria:
- [ ] A cross-file trace question produces an answer citing ≥3 files in correct call order
- [ ] Tool trail is visible in the UI and every tool call is bounded
- [ ] Path traversal attempts (`../../etc/passwd`) are rejected — test this explicitly
- [ ] Repo-overview question answers correctly using summaries without reading source files
- [ ] Follow-up questions resolve pronouns correctly

---

### Phase 4 — Evaluation and incremental indexing

**Goal:** be able to prove that changes improve quality.

Tasks:
1. **Golden sets** (`eval/golden/*.yaml`) — 3 repos × ~25 questions, spread across all five routes:
   ```yaml
   - id: flask-001
     question: "Where is the routing table built?"
     route: locate
     expected_files: ["src/flask/app.py", "src/flask/blueprints.py"]
     expected_substrings: ["url_map", "add_url_rule"]
     notes: "must mention Werkzeug's Map"
   ```
2. **Metrics** (`eval/run_eval.py`, `POST /api/eval/run`):
   - `recall@k` — did an expected file appear in the final context? (the primary retrieval metric — isolates retrieval bugs from generation bugs)
   - `citation_validity` — fraction of citations that resolve
   - `answer_correctness` — LLM-as-judge (0/1/2) against `expected_substrings` + notes, with a sample manually spot-checked
   - `latency_p50/p95`, `tokens`, `cost_per_query`, `index_time_per_mb`
3. Reports written to `eval/report/{timestamp}.json` + a markdown diff vs. the previous run.
4. **Incremental reindex**: `git fetch` + diff indexed SHA vs HEAD → reprocess only changed/added/deleted files by `content_hash`. Delete orphaned chunks, symbols, refs, and vectors.
5. Add a CI job running the eval on the smallest fixture repo.

Acceptance criteria:
- [ ] `python -m app.eval.run_eval --repo flask` produces a full metrics report
- [ ] Report shows recall@10 and correctness broken down per route
- [ ] Reindexing after a 1-file change touches only that file's rows and takes <5 seconds
- [ ] A deliberately degraded retriever (dense-only) shows a measurably lower recall — proving the harness is sensitive

---

### Phase 5 — Productionization

Tasks:
1. Frontend polish: split-pane layout (chat left, code viewer right), syntax highlighting, keyboard nav, dark mode, streaming token rendering, agent trail collapse/expand, repo switcher, error states, empty states.
2. Auth for private repos: GitHub OAuth device flow or PAT, tokens encrypted at rest, never logged or returned by the API.
3. Secret redaction on ingest: scan chunks for high-entropy strings and known key formats; redact before storing and before sending to any model.
4. Rate limiting, request timeouts, structured logging with a `query_id` trace.
5. Dockerfile + docker-compose (backend, frontend, shared volume).
6. `README.md`: setup, env vars, architecture diagram, eval results table, known limitations.

---

## 7. Implementation rules

### 7.1 File filtering (phase 0, gets it right once)

Skip: `.git`, `node_modules`, `vendor`, `dist`, `build`, `target`, `.venv`, `venv`, `__pycache__`, `.next`, `.nuxt`, `coverage`, `.mypy_cache`, `.pytest_cache`, `migrations` (optional flag).
Skip files: lockfiles (`package-lock.json`, `yarn.lock`, `poetry.lock`, `Cargo.lock`, `go.sum`), minified (`*.min.js`, `*.min.css`), generated (`*_pb2.py`, `*.pb.go`, `*.g.dart`, `*.generated.*`), binaries, images, `> 1 MB`, `> 5000 LOC`, non-UTF8, and files with average line length > 200 chars (minified/data).
Also honor `.gitignore`.
**Keep test files** — they are excellent documentation of intended usage. Mark them `is_test=1` so retrieval can down-weight them for "how does X work" but surface them for "how do I use X".

### 7.2 Provider abstraction

```python
class EmbeddingProvider(Protocol):
    dim: int
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
```
Same shape for `LLMProvider` (`complete`, `stream`, `complete_with_tools`). Business logic imports the protocol, never a vendor SDK. Tests use fakes exclusively — **no test may make a network call.**

### 7.3 Testing

- `tests/fixtures/mini_repo/` — a hand-built ~15-file repo with a known Python API, a known TS module, and deliberately tricky cases (a 900-line function, a file with no imports, a generated file that must be skipped). Assert against it precisely.
- Every phase adds tests for its own layer. Retrieval tests assert on ranks, not on LLM output.
- Target: chunking, filtering, extractors, citation verification, and path-jailing are all directly unit-tested.

### 7.4 Cost and performance guardrails

- Batch embedding calls (256 chunks per request), with retry + exponential backoff.
- Cache summaries and embeddings by content hash; never re-embed unchanged content.
- Hard cap indexing cost per repo via config (`MAX_CHUNKS_PER_REPO`, default 50_000) — refuse and report rather than silently spending.
- Log token usage per query into `query_log` so cost per query is always measurable.

### 7.5 Security

- Never `exec`, `import`, or run build tooling from an ingested repo.
- Path-jail every file read to the repo root; resolve symlinks and reject escapes.
- Clone with `--depth 1` and a timeout; cap total repo size (default 500 MB).
- Redact secrets before storage and before any model call (phase 5, but design storage for it now).

### 7.6 Conventions

- Type hints everywhere; `ruff` + `mypy` clean.
- No business logic in `api/` route handlers — they validate, delegate, serialize.
- All tunables (`RRF_K`, `TOP_K_DENSE`, `TOP_K_LEXICAL`, `MAX_AGENT_ITERATIONS`, `CONTEXT_TOKEN_BUDGET`, chunk sizes, model IDs) live in `config.py`, never inline.
- Prompts live in dedicated `prompts.py` modules as named constants, never inline f-strings in logic.

---

## 8. Recommended test repos

| Repo | Why |
|---|---|
| `tests/fixtures/mini_repo` | fast, deterministic, unit tests |
| `pallets/flask` | mid-size Python, clear structure, decorator-based routing |
| `tiangolo/fastapi` | larger Python, heavy decorators and type usage |
| `expressjs/express` | JS, different routing idiom, tests hybrid retrieval on a second language |

---

## 9. Definition of done

The project is complete when a new user can point it at an unfamiliar GitHub repo and, within a few minutes of indexing, get correct, verifiably-cited answers to all five question categories — and there is an eval report proving it, with per-route recall and correctness numbers.

---

## 10. Working agreement for the implementing agent

1. Work **one phase at a time**. At the start of a phase, restate its goal and list the files you will create or change; wait for confirmation before writing code.
2. At the end of a phase, run the tests, walk through the acceptance criteria one by one, and report which pass. Do not mark a box passed without evidence.
3. If a design decision in this spec turns out to be wrong during implementation, **say so and propose an alternative** rather than silently diverging. Note the change in a `DECISIONS.md`.
4. Prefer small, readable modules over clever ones. This codebase's retrieval logic is meant to be read and tuned by hand.
5. Never mock away a hard part to make a test pass. If something doesn't work, report it.