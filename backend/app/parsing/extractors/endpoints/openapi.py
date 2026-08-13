"""OpenAPI/Swagger spec parsing (SPEC.md §6 Phase 2 task 4): parse a
committed `openapi.yaml`/`swagger.json` directly rather than inferring
routes from code. `line` is always 1 here — a spec document doesn't have a
meaningful "line where this route is implemented" the way source code
does; `source="openapi"` tells callers not to expect one.
"""

from __future__ import annotations

import json

import yaml

from app.parsing.extractors.endpoints.common import Endpoint

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}


def extract(text: str, file_path: str) -> list[Endpoint]:
    data = _load(text, file_path)
    if not isinstance(data, dict):
        return []
    paths = data.get("paths")
    if not isinstance(paths, dict):
        return []

    out: list[Endpoint] = []
    for route, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            auth_hint = "auth" if operation.get("security") else None
            out.append(Endpoint(method.upper(), route, "openapi", operation_id, 1, auth_hint, None, "openapi"))
    return out


def _load(text: str, file_path: str) -> object:
    if file_path.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None
