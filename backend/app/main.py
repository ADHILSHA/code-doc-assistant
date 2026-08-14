"""FastAPI app, CORS, router mounting."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import browse, evaluate, query, repos
from app.config import get_settings
from app.db import get_registry_connection


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
    app.include_router(repos.router, prefix="/api")
    app.include_router(query.router, prefix="/api")
    app.include_router(browse.router, prefix="/api")
    app.include_router(evaluate.router, prefix="/api")
    return app


app = create_app()
