"""Express route extraction (SPEC.md §6 Phase 2 task 4).

Deliberately doesn't track which identifier is actually an Express `app`/
`Router()` instance — instead, any `<obj>.<verb>(...)` call whose first
argument is a string starting with "/" is treated as a route. This is a
heuristic, not a guarantee (something unrelated with a same-named method
could in principle match), but requiring a literal path-shaped first
argument makes false positives very unlikely in practice, and it means
routes are found regardless of what the app/router variable happens to be
named — no prefix/mount-point merging is attempted (`app.use(prefix,
router)`); see DECISIONS.md for why that's out of scope.
"""

from __future__ import annotations

import re

from app.parsing.extractors.endpoints.common import Endpoint

_HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head", "all"}
_AUTH_NAME_RE = re.compile(r"auth", re.IGNORECASE)


def extract(text: str, file_path: str) -> list[Endpoint]:
    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    language = "typescript" if file_path.endswith((".ts", ".tsx")) else "javascript"
    source = text.encode("utf-8")
    tree = get_parser(language).parse(source)
    if tree.root_node.has_error:
        return []

    endpoints: list[Endpoint] = []
    _walk(tree.root_node, source, endpoints)
    return endpoints


def _line_number(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def _strip_js_string(raw: str) -> str:
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _walk(node, source: bytes, out: list[Endpoint]) -> None:
    if node.type == "call_expression":
        func = node.child_by_field_name("function")
        if func is not None and func.type == "member_expression":
            prop = func.child_by_field_name("property")
            method_name = prop.text.decode() if prop is not None else None
            if method_name in _HTTP_METHODS:
                args_node = node.child_by_field_name("arguments")
                named_args = [c for c in args_node.children if c.is_named] if args_node is not None else []
                if len(named_args) >= 2 and named_args[0].type == "string":
                    path = _strip_js_string(named_args[0].text.decode("utf-8", errors="replace"))
                    if path.startswith("/"):
                        middleware = named_args[1:-1]
                        handler = named_args[-1]
                        auth_hint = next(
                            (
                                mw.text.decode()
                                for mw in middleware
                                if mw.type == "identifier" and _AUTH_NAME_RE.search(mw.text.decode())
                            ),
                            None,
                        )
                        handler_symbol = handler.text.decode() if handler.type == "identifier" else None
                        out.append(
                            Endpoint(
                                method=None if method_name == "all" else method_name.upper(),
                                route=path,
                                framework="express",
                                handler_symbol=handler_symbol,
                                line=_line_number(source, node.start_byte),
                                auth_hint=auth_hint,
                                params_json=None,
                                source="ast",
                            )
                        )
    for child in node.children:
        _walk(child, source, out)
