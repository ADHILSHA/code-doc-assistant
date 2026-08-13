"""FastAPI + Flask endpoint extraction (SPEC.md §6 Phase 2 task 4). Both are
Python decorator-based frameworks with the same shape — a call decorator
of the form `@<var>.<verb_or_route>(...)` on a function, where `<var>` was
built from `APIRouter(prefix=...)` / `Flask(...)` / `Blueprint(url_prefix=...)`
— so one extractor covers both, distinguishing them only by which factory
call built the receiver (recorded as `framework` per-endpoint, since a
single file could in principle mix them, however unlikely).
"""

from __future__ import annotations

from app.parsing.extractors.endpoints.common import Endpoint, strip_py_string

_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "options", "head"}
# factory call name -> (its prefix kwarg name, the framework label to record)
_ROUTER_FACTORIES = {
    "APIRouter": ("prefix", "fastapi"),
    "Flask": ("prefix", "flask"),
    "Blueprint": ("url_prefix", "flask"),
}


def extract(text: str, file_path: str) -> list[Endpoint]:
    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    source = text.encode("utf-8")
    tree = get_parser("python").parse(source)
    if tree.root_node.has_error:
        return []

    prefixes: dict[str, tuple[str, str]] = {}  # var name -> (prefix, framework)
    _collect_prefixes(tree.root_node, prefixes)

    endpoints: list[Endpoint] = []
    _find_routes(tree.root_node, source, prefixes, endpoints)
    return endpoints


def _line_number(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def _join_route(prefix: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if not prefix:
        return path
    return prefix.rstrip("/") + path or "/"


def _kwarg_value(call_node, kwarg_name: str):
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.children:
        if child.type == "keyword_argument":
            name_node = child.child_by_field_name("name")
            if name_node is not None and name_node.text.decode() == kwarg_name:
                return child.child_by_field_name("value")
    return None


def _kwarg_string(call_node, kwarg_name: str) -> str | None:
    value = _kwarg_value(call_node, kwarg_name)
    if value is not None and value.type == "string":
        return strip_py_string(value.text.decode("utf-8", errors="replace"))
    return None


def _first_positional_string(call_node) -> str | None:
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.children:
        if not child.is_named:
            continue
        return strip_py_string(child.text.decode("utf-8", errors="replace")) if child.type == "string" else None
    return None


def _collect_prefixes(node, prefixes: dict[str, tuple[str, str]]) -> None:
    if node.type == "assignment":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None and right is not None and left.type == "identifier" and right.type == "call":
            func = right.child_by_field_name("function")
            factory_name = func.text.decode() if func is not None else None
            if factory_name in _ROUTER_FACTORIES:
                kwarg_name, framework = _ROUTER_FACTORIES[factory_name]
                prefixes[left.text.decode()] = (_kwarg_string(right, kwarg_name) or "", framework)
    for child in node.children:
        _collect_prefixes(child, prefixes)


def _methods_kwarg(call_node) -> list[str] | None:
    value = _kwarg_value(call_node, "methods")
    if value is None or value.type != "list":
        return None
    methods = [
        strip_py_string(c.text.decode("utf-8", errors="replace")).upper()
        for c in value.children
        if c.type == "string"
    ]
    return methods or None


def _find_routes(node, source: bytes, prefixes: dict[str, tuple[str, str]], out: list[Endpoint]) -> None:
    if node.type == "decorated_definition":
        func_node = next((c for c in node.children if c.type == "function_definition"), None)
        handler_name = None
        if func_node is not None:
            name_node = func_node.child_by_field_name("name")
            handler_name = name_node.text.decode() if name_node else None

        for decorator in (c for c in node.children if c.type == "decorator"):
            call_node = next((c for c in decorator.children if c.type == "call"), None)
            if call_node is None:
                continue
            func = call_node.child_by_field_name("function")
            if func is None or func.type != "attribute":
                continue
            receiver = func.child_by_field_name("object")
            attr = func.child_by_field_name("attribute")
            if receiver is None or attr is None or receiver.type != "identifier":
                continue
            receiver_name = receiver.text.decode()
            if receiver_name not in prefixes:
                continue  # not a decorator on a tracked router/app/blueprint variable
            prefix, framework = prefixes[receiver_name]
            attr_name = attr.text.decode()

            path = _first_positional_string(call_node)
            if path is None:
                continue
            route = _join_route(prefix, path)
            line = _line_number(source, decorator.start_byte)
            auth_hint = "auth" if _kwarg_value(call_node, "dependencies") is not None else None

            if attr_name == "route":  # Flask's generic decorator
                for method in _methods_kwarg(call_node) or ["GET"]:
                    out.append(
                        Endpoint(method, route, framework, handler_name, line, auth_hint, None, source="ast")
                    )
            elif attr_name in _HTTP_VERBS:
                out.append(
                    Endpoint(attr_name.upper(), route, framework, handler_name, line, auth_hint, None, source="ast")
                )
    for child in node.children:
        _find_routes(child, source, prefixes, out)
