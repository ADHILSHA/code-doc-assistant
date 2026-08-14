"""FastAPI app, CORS, router mounting."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, browse, evaluate, query, repos
from app.config import get_settings
from app.db import get_registry_connection
from app.logging_setup import configure_logging
from app.middleware import RateLimitMiddleware, RequestContextMiddleware

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.clones_dir.mkdir(parents=True, exist_ok=True)
    settings.dbs_dir.mkdir(parents=True, exist_ok=True)
    get_registry_connection(settings).close()  # ensure registry db + schema exist
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Code Documentation Assistant", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # SPEC.md §6 Phase 5 task 4. Added in this order so RequestContextMiddleware
    # ends up outermost (Starlette wraps middleware in reverse-add order —
    # see app/middleware.py's docstring / this decision's rationale in
    # DECISIONS.md): it must see every request first to assign the
    # request_id every other layer's structured log lines key off of, and
    # last on the way out so its "request completed" log line reflects the
    # real final status code, including a 429 from RateLimitMiddleware.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(repos.router, prefix="/api")
    app.include_router(query.router, prefix="/api")
    app.include_router(browse.router, prefix="/api")
    app.include_router(evaluate.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    return app


app = create_app()
