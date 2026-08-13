"""NestJS route extraction (SPEC.md §6 Phase 2 task 4): `@Controller(prefix)`
class + `@Get()`/`@Post()`/... method decorators.

Structurally different from chunker.py's decorator handling: a TS
*method*-level decorator is a preceding **sibling** of the `method_definition`
inside `class_body`, not a wrapper around it (verified directly — this
surprised me given Phase 1's class-level decorator finding, so I checked
rather than assumed). A *class*-level decorator on an exported class is
also not a child of `class_declaration` — it's a sibling inside the
enclosing `export_statement`. Both are handled explicitly below rather
than via `parsing/chunker.py`'s `unwrap_decorator` (built for the wrapper
shape, not the sibling shape).
"""

from __future__ import annotations

from app.parsing.extractors.endpoints.common import Endpoint

_VERB_DECORATORS = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH"}


def extract(text: str, file_path: str) -> list[Endpoint]:
    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    language = "typescript" if file_path.endswith((".ts", ".tsx")) else "javascript"
    source = text.encode("utf-8")
    tree = get_parser(language).parse(source)
    if tree.root_node.has_error:
        return []
    out: list[Endpoint] = []
    _walk(tree.root_node, source, out)
    return out


def _line_number(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def _strip_js_string(raw: str) -> str:
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _decorator_call(decorator_node):
    return next((c for c in decorator_node.children if c.type == "call_expression"), None)


def _decorator_name(decorator_node) -> str | None:
    call = _decorator_call(decorator_node)
    if call is None:
        # a bare `@Injectable` with no call parens
        ident = next((c for c in decorator_node.children if c.type == "identifier"), None)
        return ident.text.decode() if ident is not None else None
    func = call.child_by_field_name("function")
    return func.text.decode() if func is not None else None


def _decorator_first_string_arg(decorator_node) -> str | None:
    call = _decorator_call(decorator_node)
    if call is None:
        return None
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    first = next((c for c in args.children if c.is_named), None)
    if first is not None and first.type == "string":
        return _strip_js_string(first.text.decode("utf-8", errors="replace"))
    return None


def _class_decorators(class_node) -> list:
    decorators = [c for c in class_node.children if c.type == "decorator"]
    parent = class_node.parent
    if parent is not None and parent.type == "export_statement":
        decorators += [c for c in parent.children if c.type == "decorator"]
    return decorators


def _join_route(prefix: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if not prefix:
        return path
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return (prefix.rstrip("/") + path) or "/"


def _walk(node, source: bytes, out: list[Endpoint]) -> None:
    if node.type == "class_declaration":
        controller_decorator = next(
            (d for d in _class_decorators(node) if _decorator_name(d) == "Controller"), None
        )
        if controller_decorator is not None:
            prefix = _decorator_first_string_arg(controller_decorator) or ""
            body = node.child_by_field_name("body")
            if body is not None:
                _walk_class_body(body, source, prefix, out)
        # still recurse in case of nested classes/functions
    for child in node.children:
        _walk(child, source, out)


def _walk_class_body(body, source: bytes, prefix: str, out: list[Endpoint]) -> None:
    pending: list = []
    for child in body.children:
        if child.type == "decorator":
            pending.append(child)
            continue
        if child.type == "method_definition" and pending:
            route_decorator = next((d for d in pending if _decorator_name(d) in _VERB_DECORATORS), None)
            if route_decorator is not None:
                verb_name = _decorator_name(route_decorator)
                assert verb_name is not None  # guaranteed by the `next(...)` filter above
                method = _VERB_DECORATORS[verb_name]
                path = _decorator_first_string_arg(route_decorator) or ""
                route = _join_route(prefix, path)
                name_node = child.child_by_field_name("name")
                handler = name_node.text.decode() if name_node is not None else None
                auth_hint = "auth" if any(_decorator_name(d) in ("UseGuards", "Auth") for d in pending) else None
                out.append(
                    Endpoint(method, route, "nestjs", handler, _line_number(source, route_decorator.start_byte), auth_hint, None, "ast")
                )
        pending = []
