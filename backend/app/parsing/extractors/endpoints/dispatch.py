"""Dispatches to the right framework-specific endpoint extractor(s) for a
file, based on language and (for Rails/OpenAPI) filename convention.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from app.parsing.extractors.endpoints import (
    django_urls,
    express,
    fastapi_flask,
    nestjs,
    nextjs,
    openapi,
    rails,
    spring,
)
from app.parsing.extractors.endpoints.common import Endpoint

_OPENAPI_FILENAMES = {"openapi.yaml", "openapi.yml", "swagger.yaml", "swagger.yml", "swagger.json"}


def extract_endpoints(text: str, file_path: str, language: str | None) -> list[Endpoint]:
    name = PurePosixPath(file_path).name

    if name in _OPENAPI_FILENAMES:
        return openapi.extract(text, file_path)

    if language == "python":
        return [*fastapi_flask.extract(text, file_path), *django_urls.extract(text, file_path)]

    if language in ("javascript", "typescript", "tsx"):
        return [
            *express.extract(text, file_path),
            *nestjs.extract(text, file_path),
            *nextjs.extract(text, file_path),
        ]

    if language == "java":
        return spring.extract(text, file_path)

    if language == "ruby" and name == "routes.rb":
        return rails.extract(text, file_path)

    return []
