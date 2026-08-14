# Multi-stage: build stage compiles/installs into a venv (needs a C
# toolchain in case any dependency lacks a prebuilt wheel for this
# platform); the runtime stage copies only that venv + the app source, so
# the final image doesn't carry a compiler around (SPEC.md §6 Phase 5
# task 5).
FROM python:3.12-slim AS build

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Copy only what pip needs to resolve dependencies first, so this layer
# stays cached across app-code-only changes.
#
# `.[embeddings]` (not bare `.`): voyageai/openai are optional extras
# (pyproject.toml) so the test suite's zero-network FakeEmbeddingProvider
# path doesn't need them — but this is the real deployed server, meant to
# run against a real EMBEDDING_PROVIDER (voyage/openai), and needs the
# actual SDKs installed. Found by actually running `docker compose up` and
# indexing a repo — the base install silently produced a working container
# that then failed every index job with `ModuleNotFoundError: No module
# named 'openai'`, not by inspecting pyproject.toml's extras split.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir '.[embeddings]'


FROM python:3.12-slim AS runtime

# git: ingest/source.py shells out to it for clone/fetch (SPEC.md §7.5).
# ripgrep (rg): the agent's grep tool's fast path — optional (there's a
# pure-Python fallback, see agent/tools.py), but this is the deployed
# server, not a dev laptop that might not have it; installing it here
# avoids every deployment silently running the slower fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ripgrep \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /venv /venv
COPY app ./app
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Overridden by docker-compose.yml — this default only matters if the
# image is run standalone without it. Must be an absolute, writable path;
# config.py's own default (relative to app/config.py's location) resolves
# to somewhere under /app otherwise, which isn't where the shared volume
# in docker-compose.yml is mounted.
ENV DATA_DIR=/app/data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
