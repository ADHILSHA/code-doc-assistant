"""Symbol extraction (SPEC.md §6 Phase 2 task 1) — populates `symbols` for
every supported language.

Reuses parsing/chunker.py's `LANGUAGE_SPECS` and its name/wrapper helpers:
"what counts as a definition" is identical for chunking and symbol
extraction (same node-type tables), only what's recorded per definition
differs — a `Chunk` carries embeddable text (and may be split if oversized);
a `Symbol` carries name/kind/signature/docstring/parent/exported for the
symbol graph, always as one row per whole definition, never split. That
difference is enough that the two walk their own AST traversal rather than
sharing one (see the walk function below vs. chunker.py's `_walk`).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.parsing.chunker import (
    CONTAINER_KIND,
    DEFINITION_KIND,
    LANGUAGE_SPECS,
    LanguageSpec,
    extract_name,
    unwrap_decorator,
)
from app.parsing.languages import has_tree_sitter_grammar


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    signature: str | None
    docstring: str | None
    parent_symbol: str | None
    start_line: int
    end_line: int
    is_exported: bool


def extract_symbols(text: str, language: str | None) -> list[Symbol]:
    """[] for unsupported languages or files tree-sitter can't cleanly
    parse — there's no naive-splitter equivalent for symbols (a fallback
    chunk has no meaningful "name")."""
    if not has_tree_sitter_grammar(language):
        return []
    assert language is not None

    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    source = text.encode("utf-8")
    tree = get_parser(language).parse(source)
    if tree.root_node.has_error:
        return []

    spec = LANGUAGE_SPECS[language]
    symbols: list[Symbol] = []
    _walk(tree.root_node, spec, language, None, symbols, source)
    return symbols


def _line_number(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def _signature(source: bytes, inner_node) -> str:
    """First line of the definition's own text — deliberately `inner_node`,
    not the decorator/export-inclusive `span_node`: found via smoke-testing
    that using `span_node` made a decorated symbol's "signature" show its
    decorator line (`@dataclass`) instead of the actual declaration."""
    text = source[inner_node.start_byte : inner_node.end_byte].decode("utf-8", errors="replace")
    first_line = text.splitlines()[0] if text else ""
    return first_line.strip()


def _strip_string_literal(raw: str) -> str:
    for quote in ('"""', "'''", '"', "'"):
        if raw.startswith(quote) and raw.endswith(quote) and len(raw) >= 2 * len(quote):
            return raw[len(quote) : -len(quote)].strip()
    return raw.strip()


def _docstring(inner_node, language: str) -> str | None:
    # Python only for now — other languages' doc-comment conventions
    # (JSDoc, Javadoc, ///, ...) live in separate comment nodes preceding
    # the definition, not inside its body, and need different handling.
    # Not tested by any Phase 2 acceptance criterion; left for later.
    if language != "python":
        return None
    body = inner_node.child_by_field_name("body")
    if body is None:
        return None
    named = [c for c in body.children if c.is_named]
    if not named:
        return None
    first = named[0]
    # A bare string statement at the top of a block is a `string` node
    # directly in this grammar version, NOT wrapped in an
    # `expression_statement` (found via a failing test, not by inspection —
    # earlier smoke-testing happened to only check the *class*-exported-flag
    # path, not docstring content, so this went unnoticed until test_symbols.py
    # asserted on an actual docstring value).
    if first.type == "expression_statement":
        first_child = first.children[0] if first.children else None
    elif first.type == "string":
        first_child = first
    else:
        return None
    if first_child is None or first_child.type != "string":
        return None
    return _strip_string_literal(first_child.text.decode("utf-8", errors="replace"))


def _is_exported(name: str, inner_node, language: str, was_export_wrapped: bool) -> bool:
    if language in ("javascript", "typescript", "tsx"):
        return was_export_wrapped
    if language == "python":
        return not name.startswith("_")
    if language == "go":
        return bool(name) and name[0].isupper()
    if language == "java":
        modifiers = [c for c in inner_node.children if c.type == "modifiers"]
        return bool(modifiers) and b"public" in modifiers[0].text
    if language == "rust":
        return any(c.type == "visibility_modifier" for c in inner_node.children)
    return True  # ruby, and anything else: no reliable convention to check


def _walk(
    node,
    spec: LanguageSpec,
    language: str,
    parent_symbol: str | None,
    symbols: list[Symbol],
    source: bytes,
    *,
    container_exported: bool = True,
) -> None:
    for child in node.children:
        if not child.is_named:
            continue
        span_node, inner_node = unwrap_decorator(child, spec.wrapper_types)
        was_wrapped = span_node is not inner_node

        if inner_node.type in spec.definition_types:
            name = extract_name(inner_node)
            if name:
                exported = _is_exported(name, inner_node, language, was_wrapped)
                if language in ("javascript", "typescript", "tsx") and parent_symbol is not None:
                    # JS/TS `export` only applies at the declaration it's
                    # written on — a method of an exported class isn't
                    # itself export-wrapped. Inherit from the enclosing
                    # container instead of reporting every method as
                    # unexported (found via smoke-testing against
                    # userClient.ts, not by inspection).
                    exported = exported or container_exported
                symbols.append(
                    Symbol(
                        name=name,
                        kind=DEFINITION_KIND.get(inner_node.type, "function"),
                        signature=_signature(source, inner_node),
                        docstring=_docstring(inner_node, language),
                        parent_symbol=parent_symbol,
                        start_line=_line_number(source, span_node.start_byte),
                        end_line=_line_number(source, max(span_node.start_byte, span_node.end_byte - 1)),
                        is_exported=exported,
                    )
                )
        elif inner_node.type in spec.container_types:
            name = extract_name(inner_node)
            container_is_exported = _is_exported(name, inner_node, language, was_wrapped) if name else False
            if name:
                symbols.append(
                    Symbol(
                        name=name,
                        kind=CONTAINER_KIND.get(inner_node.type, "class"),
                        signature=_signature(source, inner_node),
                        docstring=_docstring(inner_node, language),
                        parent_symbol=parent_symbol,
                        start_line=_line_number(source, span_node.start_byte),
                        end_line=_line_number(source, max(span_node.start_byte, span_node.end_byte - 1)),
                        is_exported=container_is_exported,
                    )
                )
            _walk(inner_node, spec, language, name, symbols, source, container_exported=container_is_exported)
        else:
            _walk(child, spec, language, parent_symbol, symbols, source, container_exported=container_exported)
