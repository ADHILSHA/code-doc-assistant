"""Spring (Java) route extraction (SPEC.md §6 Phase 2 task 4):
`@RequestMapping`/`@GetMapping`/etc. annotations on a `@RestController`/
`@Controller` class and its methods. Annotations are children of a
`modifiers` node on the class/method itself (verified directly) — simpler
than NestJS's TS-decorator-as-sibling shape.
"""

from __future__ import annotations

from app.parsing.extractors.endpoints.common import Endpoint

_VERB_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
_AUTH_ANNOTATIONS = {"PreAuthorize", "Secured", "RolesAllowed"}


def extract(text: str, file_path: str) -> list[Endpoint]:
    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    source = text.encode("utf-8")
    tree = get_parser("java").parse(source)
    if tree.root_node.has_error:
        return []
    out: list[Endpoint] = []
    _walk(tree.root_node, source, out)
    return out


def _line_number(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def _annotations(node) -> list:
    modifiers = next((c for c in node.children if c.type == "modifiers"), None)
    if modifiers is None:
        return []
    return [c for c in modifiers.children if c.type in ("annotation", "marker_annotation")]


def _annotation_name(ann) -> str | None:
    ident = next((c for c in ann.children if c.type == "identifier"), None)
    return ident.text.decode() if ident is not None else None


def _strip_java_string(raw: str) -> str:
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


def _first_string_literal(node) -> str | None:
    if node.type == "string_literal":
        return _strip_java_string(node.text.decode("utf-8", errors="replace"))
    for child in node.children:
        found = _first_string_literal(child)
        if found is not None:
            return found
    return None


def _annotation_path(ann) -> str | None:
    args = next((c for c in ann.children if c.type == "annotation_argument_list"), None)
    return _first_string_literal(args) if args is not None else None


def _join_route(prefix: str, path: str) -> str:
    if path and not path.startswith("/"):
        path = "/" + path
    if not prefix:
        return path or "/"
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    return (prefix.rstrip("/") + path) or "/"


def _walk(node, source: bytes, out: list[Endpoint]) -> None:
    if node.type == "class_declaration":
        class_anns = _annotations(node)
        if any(_annotation_name(a) in ("RestController", "Controller") for a in class_anns):
            mapping = next((a for a in class_anns if _annotation_name(a) == "RequestMapping"), None)
            prefix = (_annotation_path(mapping) or "") if mapping is not None else ""
            body = node.child_by_field_name("body")
            if body is not None:
                for child in body.children:
                    if child.type == "method_declaration":
                        _emit_method_route(child, source, prefix, out)
    for child in node.children:
        _walk(child, source, out)


def _emit_method_route(method_node, source: bytes, prefix: str, out: list[Endpoint]) -> None:
    anns = _annotations(method_node)
    name_node = method_node.child_by_field_name("name")
    handler = name_node.text.decode() if name_node is not None else None
    auth_hint = "auth" if any(_annotation_name(a) in _AUTH_ANNOTATIONS for a in anns) else None

    for ann in anns:
        name = _annotation_name(ann)
        route = _join_route(prefix, _annotation_path(ann) or "")
        line = _line_number(source, ann.start_byte)
        if name in _VERB_ANNOTATIONS:
            out.append(Endpoint(_VERB_ANNOTATIONS[name], route, "spring", handler, line, auth_hint, None, "ast"))
        elif name == "RequestMapping":
            out.append(Endpoint(None, route, "spring", handler, line, auth_hint, None, "ast"))
