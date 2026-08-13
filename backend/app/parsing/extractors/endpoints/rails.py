"""Rails `config/routes.rb` route extraction (SPEC.md §6 Phase 2 task 4).

Regex-based, not AST-based — Ruby's routing DSL is declarative enough that
matching the common forms (explicit verb calls, `resources`/`resource`)
line-by-line is about as accurate as parsing the full Ruby AST would be
for this specific, constrained subset. Known simplification: routes
nested inside a `namespace :api do ... end` block don't get the block's
prefix merged in (no block-scope tracking) — each line is read independently.
"""

from __future__ import annotations

import re

from app.parsing.extractors.endpoints.common import Endpoint

_VERB_LINE_RE = re.compile(r"""^\s*(get|post|put|patch|delete)\s+['"]([^'"]+)['"](?:.*?to:\s*['"]([^'"]+)['"])?""")
_RESOURCES_RE = re.compile(r"^\s*(resources?)\s+:(\w+)")

# (method, path suffix, action name)
_PLURAL_ACTIONS = [
    ("GET", "", "index"),
    ("GET", "new", "new"),
    ("POST", "", "create"),
    ("GET", ":id", "show"),
    ("GET", ":id/edit", "edit"),
    ("PATCH", ":id", "update"),
    ("DELETE", ":id", "destroy"),
]
_SINGULAR_ACTIONS = [
    ("GET", "new", "new"),
    ("POST", "", "create"),
    ("GET", "", "show"),
    ("GET", "edit", "edit"),
    ("PATCH", "", "update"),
    ("DELETE", "", "destroy"),
]


def extract(text: str, file_path: str) -> list[Endpoint]:
    out: list[Endpoint] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        verb_match = _VERB_LINE_RE.match(raw_line)
        if verb_match:
            method, path, handler = verb_match.groups()
            route = "/" + path.lstrip("/")
            out.append(Endpoint(method.upper(), route, "rails", handler, line_no, None, None, "ast"))
            continue

        resources_match = _RESOURCES_RE.match(raw_line)
        if resources_match:
            keyword, name = resources_match.groups()
            actions = _PLURAL_ACTIONS if keyword == "resources" else _SINGULAR_ACTIONS
            base = f"/{name}"
            for method, suffix, action in actions:
                route = base if not suffix else f"{base}/{suffix}"
                out.append(Endpoint(method, route, "rails", f"{name}#{action}", line_no, None, None, "ast"))
    return out
