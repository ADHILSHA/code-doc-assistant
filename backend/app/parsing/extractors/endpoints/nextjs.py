"""Next.js file-system route extraction (SPEC.md §6 Phase 2 task 4):
`pages/api/**` and `app/**/route.ts` are routes by file *path* convention,
not by any decorator/call pattern — so this extractor works off `file_path`
more than AST content. For `app/**/route.ts`, the exported function names
(`GET`, `POST`, ...) are still found with a regex over the source rather
than a full AST parse — matching top-level `export function NAME` /
`export const NAME =` is unambiguous enough here not to need one.
"""

from __future__ import annotations

import re

from app.parsing.extractors.endpoints.common import Endpoint

_APP_ROUTE_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}
_EXPORT_FUNC_RE = re.compile(r"^export\s+(?:async\s+)?function\s+(\w+)", re.MULTILINE)
_EXPORT_CONST_RE = re.compile(r"^export\s+const\s+(\w+)\s*=", re.MULTILINE)
_APP_ROUTE_FILE_RE = re.compile(r"(^|/)app/.*/route\.(ts|js|tsx|jsx)$")


def extract(text: str, file_path: str) -> list[Endpoint]:
    normalized = file_path.replace("\\", "/")

    if "/pages/api/" in f"/{normalized}":
        route = _pages_api_route(normalized)
        return [Endpoint(None, route, "nextjs", None, 1, None, None, "ast")]

    if _APP_ROUTE_FILE_RE.search(normalized):
        route = _app_router_route(normalized)
        endpoints: list[Endpoint] = []
        for pattern in (_EXPORT_FUNC_RE, _EXPORT_CONST_RE):
            for match in pattern.finditer(text):
                name = match.group(1)
                if name in _APP_ROUTE_METHODS:
                    line = text.count("\n", 0, match.start()) + 1
                    endpoints.append(Endpoint(name, route, "nextjs", name, line, None, None, "ast"))
        return endpoints or [Endpoint(None, route, "nextjs", None, 1, None, None, "ast")]

    return []


def _pages_api_route(file_path: str) -> str:
    route = file_path.split("pages", 1)[-1].rsplit(".", 1)[0]
    route = route.removesuffix("/index")
    return route or "/"


def _app_router_route(file_path: str) -> str:
    route = file_path.split("app", 1)[-1].rsplit("/route.", 1)[0]
    return route or "/"
