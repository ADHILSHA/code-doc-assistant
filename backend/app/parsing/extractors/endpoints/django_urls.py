"""Django `urls.py` route extraction (SPEC.md §6 Phase 2 task 4).

`path()`/`re_path()`/`url()` calls, wherever they appear (not just inside a
literal `urlpatterns = [...]` list, since `+`-concatenated or conditionally
built pattern lists are common). Django views handle their own method
dispatch internally, so `method` is always None here — the route itself
doesn't declare one.
"""

from __future__ import annotations

from app.parsing.extractors.endpoints.common import Endpoint, strip_py_string

_ROUTE_FUNCS = {"path", "re_path", "url"}


def extract(text: str, file_path: str) -> list[Endpoint]:
    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    source = text.encode("utf-8")
    tree = get_parser("python").parse(source)
    if tree.root_node.has_error:
        return []
    endpoints: list[Endpoint] = []
    _walk(tree.root_node, source, endpoints)
    return endpoints


def _line_number(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def _view_name(node) -> str | None:
    if node is None:
        return None
    if node.type == "identifier":
        return node.text.decode()
    if node.type == "attribute":
        attr = node.child_by_field_name("attribute")
        return attr.text.decode() if attr is not None else None
    if node.type == "call":  # SomeView.as_view()
        return _view_name(node.child_by_field_name("function"))
    return None


def _walk(node, source: bytes, out: list[Endpoint]) -> None:
    if node.type == "call":
        func = node.child_by_field_name("function")
        func_name = func.text.decode() if func is not None else None
        if func_name in _ROUTE_FUNCS:
            args = node.child_by_field_name("arguments")
            named = [c for c in args.children if c.is_named] if args is not None else []
            if named and named[0].type == "string":
                route = strip_py_string(named[0].text.decode("utf-8", errors="replace"))
                route = "/" + route.lstrip("/").lstrip("^").rstrip("$")
                handler = _view_name(named[1]) if len(named) > 1 else None
                out.append(
                    Endpoint(None, route, "django", handler, _line_number(source, node.start_byte), None, None, "ast")
                )
    for child in node.children:
        _walk(child, source, out)
