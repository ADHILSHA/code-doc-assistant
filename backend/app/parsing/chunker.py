"""AST-aware chunking (SPEC.md §6 Phase 1 task 1).

Chunk granularity (a design decision not spelled out in the spec's prose —
see DECISIONS.md): each function/method becomes its own chunk, tagged with
`parent_symbol` for its enclosing class/struct/impl/interface. Each
container (class/struct/interface/impl) additionally gets a lightweight
"shell" chunk — its signature plus whatever body content precedes its first
nested method (docstring, attributes, fields) — rather than duplicating the
whole class body into every method's chunk. Module-level code not covered
by any top-level definition (imports, constants, a trailing `__main__`
block, ...) becomes its own "module" chunk(s), per SPEC.md's "module-level
code becomes one chunk".

Functions over `max_tokens` split on statement boundaries (tree-sitter's
named children of the body block) with `overlap_statements` of overlap.

Markdown gets heading-based text splitting, not a tree-sitter grammar.
Unsupported languages, and any file tree-sitter can't cleanly parse
(`tree.root_node.has_error`), fall back to `naive_chunk_text` — the same
fixed-window splitter Phase 0 used for everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.parsing.languages import has_tree_sitter_grammar, is_markdown

_HEADING_RE = re.compile(r"^#{1,6}\s")


@dataclass(frozen=True)
class Chunk:
    start_line: int  # 1-indexed, inclusive
    end_line: int  # 1-indexed, inclusive
    content: str
    symbol_name: str | None
    symbol_kind: str | None  # function|method|class|struct|interface|impl|module|markdown_section
    parent_symbol: str | None


@dataclass(frozen=True)
class ChunkingResult:
    chunks: list[Chunk]
    strategy: str  # "ast" | "markdown" | "naive"


@dataclass(frozen=True)
class LanguageSpec:
    # Function/method-like nodes: each becomes its own chunk. Not recursed
    # into further — statements nested inside a function body don't get
    # their own chunk boundaries (Phase 1 scope).
    definition_types: frozenset[str]
    # Class/struct/interface/impl-like nodes: recursed into to find nested
    # definitions, and get their own "shell" chunk (see module docstring).
    container_types: frozenset[str]
    # Nodes that wrap a definition/container without being one themselves
    # (Python's `@decorator` puts the real class/function one level down
    # inside a `decorated_definition` node). Without unwrapping these, the
    # decorator's lines would double-count: once in the module/shell
    # "preamble" that precedes the wrapped node, and again failing to be
    # excluded from it — an actual overlap bug, not just a minor gap (found
    # by smoke-testing against a real file, not by inspection). JS/TS
    # decorators and Java annotations don't need this — verified those
    # attach as a direct child of the definition node itself, already
    # inside its span. Rust's `#[attribute]` is a *preceding sibling*, not
    # a wrapper, so it isn't fixable the same way; left as a known small
    # gap (the attribute line is neither duplicated nor included) — see
    # DECISIONS.md.
    wrapper_types: frozenset[str] = frozenset()


LANGUAGE_SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        definition_types=frozenset({"function_definition"}),
        container_types=frozenset({"class_definition"}),
        wrapper_types=frozenset({"decorated_definition"}),
    ),
    "javascript": LanguageSpec(
        definition_types=frozenset(
            {"function_declaration", "method_definition", "generator_function_declaration"}
        ),
        container_types=frozenset({"class_declaration"}),
        wrapper_types=frozenset({"export_statement"}),
    ),
    "typescript": LanguageSpec(
        definition_types=frozenset(
            {"function_declaration", "method_definition", "generator_function_declaration"}
        ),
        container_types=frozenset({"class_declaration", "interface_declaration"}),
        wrapper_types=frozenset({"export_statement"}),
    ),
    "tsx": LanguageSpec(
        definition_types=frozenset(
            {"function_declaration", "method_definition", "generator_function_declaration"}
        ),
        container_types=frozenset({"class_declaration", "interface_declaration"}),
        wrapper_types=frozenset({"export_statement"}),
    ),
    "go": LanguageSpec(
        # Go doesn't nest methods inside type declarations (methods are
        # separate top-level declarations with a receiver), so struct/
        # interface types are treated as leaf definitions, not containers.
        definition_types=frozenset({"function_declaration", "method_declaration", "type_declaration"}),
        container_types=frozenset(),
    ),
    "java": LanguageSpec(
        definition_types=frozenset({"method_declaration", "constructor_declaration"}),
        container_types=frozenset({"class_declaration", "interface_declaration"}),
    ),
    "rust": LanguageSpec(
        definition_types=frozenset({"function_item"}),
        container_types=frozenset({"struct_item", "impl_item", "trait_item", "enum_item"}),
    ),
    "ruby": LanguageSpec(
        definition_types=frozenset({"method", "singleton_method"}),
        container_types=frozenset({"class", "module"}),
    ),
}

_CONTAINER_KIND = {
    "class_definition": "class",
    "class_declaration": "class",
    "class": "class",
    "interface_declaration": "interface",
    "struct_item": "struct",
    "impl_item": "impl",
    "trait_item": "trait",
    "enum_item": "enum",
    "module": "module",
}

_DEFINITION_KIND = {
    "function_definition": "function",
    "function_declaration": "function",
    "function_item": "function",
    "generator_function_declaration": "function",
    "method_definition": "method",
    "method_declaration": "method",
    "method": "method",
    "singleton_method": "method",
    "constructor_declaration": "method",
    "type_declaration": "type",
}


def naive_chunk_text(
    text: str, *, size: int = 1500, overlap: int = 200
) -> list[tuple[int, int, str]]:
    """Fixed-size character windows with overlap — the Phase 0 splitter,
    kept as the designated fallback for unsupported languages and
    unparseable files. Returns (start_line, end_line, content) tuples,
    1-indexed inclusive line numbers.
    """
    if not text:
        return []
    n = len(text)

    def line_at(idx: int) -> int:
        return text.count("\n", 0, idx) + 1

    if n <= size:
        return [(1, line_at(n - 1), text)]

    step = max(size - overlap, 1)
    windows: list[tuple[int, int, str]] = []
    pos = 0
    while pos < n:
        end = min(pos + size, n)
        windows.append((line_at(pos), line_at(end - 1), text[pos:end]))
        if end == n:
            break
        pos += step
    return windows


def chunk_file(
    text: str,
    language: str | None,
    *,
    max_tokens: int = 800,
    overlap_statements: int = 1,
    naive_size: int = 1500,
    naive_overlap: int = 200,
) -> ChunkingResult:
    """Single entry point index/store.py uses regardless of language."""
    if is_markdown(language):
        return ChunkingResult(
            chunks=_chunk_markdown(text, max_tokens=max_tokens, naive_size=naive_size, naive_overlap=naive_overlap),
            strategy="markdown",
        )

    if has_tree_sitter_grammar(language):
        assert language is not None
        ast_chunks = _chunk_ast(text, language, max_tokens=max_tokens, overlap_statements=overlap_statements)
        if ast_chunks is not None:
            return ChunkingResult(chunks=ast_chunks, strategy="ast")

    naive = [
        Chunk(start, end, content, symbol_name=None, symbol_kind="module", parent_symbol=None)
        for start, end, content in naive_chunk_text(text, size=naive_size, overlap=naive_overlap)
    ]
    return ChunkingResult(chunks=naive, strategy="naive")


# --- AST-based chunking ---


def _line_number(source: bytes, byte_offset: int) -> int:
    """1-indexed line number of the byte AT `byte_offset` (not one past
    it) — see index/store.py's naive_chunk_text for why this exact-offset
    convention matters (an off-by-one here double-counts a trailing
    newline as a phantom extra line)."""
    return source.count(b"\n", 0, byte_offset) + 1


def _extract_name(node) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8", errors="replace")

    # Go: `type Foo struct {...}` — the name lives on the nested type_spec,
    # not on type_declaration itself.
    if node.type == "type_declaration":
        for child in node.children:
            if child.type == "type_spec":
                spec_name = child.child_by_field_name("name")
                if spec_name is not None:
                    return spec_name.text.decode("utf-8", errors="replace")

    # Rust: impl blocks expose the implementing type via the "type" field.
    type_node = node.child_by_field_name("type")
    if type_node is not None:
        return type_node.text.decode("utf-8", errors="replace")

    return None


def _statement_children(body) -> list:
    """Named children of a function/method body block, treating a lone
    wrapper node (e.g. Go's `block` -> single `statement_list`) as
    transparent."""
    named = [c for c in body.children if c.is_named]
    if len(named) == 1 and named[0].type.endswith("_list"):
        return [c for c in named[0].children if c.is_named]
    return named


def _split_oversized(node, source: bytes, max_tokens: int, overlap_statements: int) -> list[Chunk]:
    body = node.child_by_field_name("body")
    statements = _statement_children(body) if body is not None else []
    if not statements:
        return [
            Chunk(
                _line_number(source, node.start_byte),
                _line_number(source, node.end_byte - 1),
                source[node.start_byte : node.end_byte].decode("utf-8", errors="replace"),
                symbol_name=None,
                symbol_kind=None,
                parent_symbol=None,
            )
        ]

    max_chars = max_tokens * 4
    header_len = body.start_byte - node.start_byte
    windows: list[Chunk] = []
    i = 0
    n = len(statements)
    while i < n:
        span_start_byte = node.start_byte if i == 0 else statements[i].start_byte
        cur_len = header_len if i == 0 else 0
        j = i
        while j < n:
            stmt_len = statements[j].end_byte - statements[j].start_byte
            if j > i and cur_len + stmt_len > max_chars:
                break
            cur_len += stmt_len
            j += 1
        span_end_byte = statements[j - 1].end_byte
        windows.append(
            Chunk(
                _line_number(source, span_start_byte),
                _line_number(source, span_end_byte - 1),
                source[span_start_byte:span_end_byte].decode("utf-8", errors="replace"),
                symbol_name=None,
                symbol_kind=None,
                parent_symbol=None,
            )
        )
        if j >= n:
            break
        i = max(j - overlap_statements, i + 1)
    return windows


def _unwrap(node, wrapper_types: frozenset[str]):
    """For a `child` that's a decorator-style wrapper (Python's
    `decorated_definition`), returns (span_node, inner_node): `span_node`'s
    byte range includes the decorator and should be used for the chunk's
    boundaries; `inner_node` is the actual class/function/etc. node,
    used for name/type classification and body lookup. For a plain
    (non-wrapped) node, both are the node itself."""
    if node.type in wrapper_types:
        for child in node.children:
            if child.is_named and child.type != "decorator":
                return node, child
        return node, node
    return node, node


def _emit_definition(
    inner_node,
    span_node,
    chunks: list[Chunk],
    source: bytes,
    parent_symbol: str | None,
    *,
    max_tokens: int,
    overlap_statements: int,
) -> None:
    name = _extract_name(inner_node)
    kind = _DEFINITION_KIND.get(inner_node.type, "function")
    token_estimate = (span_node.end_byte - span_node.start_byte) // 4

    if token_estimate <= max_tokens:
        chunks.append(
            Chunk(
                _line_number(source, span_node.start_byte),
                _line_number(source, span_node.end_byte - 1),
                source[span_node.start_byte : span_node.end_byte].decode("utf-8", errors="replace"),
                symbol_name=name,
                symbol_kind=kind,
                parent_symbol=parent_symbol,
            )
        )
        return

    # Oversized: split on inner_node's statement boundaries. If span_node
    # differs from inner_node (a decorated definition), the decorator text
    # is only included in this first split segment's boundary calc, not
    # its content — an accepted simplification for the rare oversized+
    # decorated case (see LanguageSpec.wrapper_types docstring).
    for split in _split_oversized(inner_node, source, max_tokens, overlap_statements):
        chunks.append(
            Chunk(split.start_line, split.end_line, split.content, symbol_name=name, symbol_kind=kind, parent_symbol=parent_symbol)
        )


def _first_definition_start(node, definition_types: frozenset[str], wrapper_types: frozenset[str]) -> int | None:
    for child in node.children:
        if not child.is_named:
            continue
        span_node, inner_node = _unwrap(child, wrapper_types)
        if inner_node.type in definition_types:
            return span_node.start_byte
        found = _first_definition_start(child, definition_types, wrapper_types)
        if found is not None:
            return found
    return None


def _trimmed_span(source: bytes, start_byte: int, end_byte: int) -> tuple[int, str] | None:
    """Slices [start_byte, end_byte), strips trailing whitespace, and
    returns (end_line, stripped_text) — or None if nothing but whitespace
    remains. `end_byte` is often "the start of whatever construct comes
    next", so the raw slice's tail is typically blank lines / indentation
    that physically sits on the *next* construct's first line; stripping
    it (rather than deriving end_line straight from `end_byte - 1`) keeps
    end_line pointing at the last line with real content, not a
    whitespace-only line that cosmetically overlaps the next chunk's
    reported start_line. Found by smoke-testing against a real file, not
    by inspection — see DECISIONS.md.
    """
    text = source[start_byte:end_byte].decode("utf-8", errors="replace")
    stripped = text.rstrip()
    if not stripped.strip():
        return None
    start_line = _line_number(source, start_byte)
    return start_line + stripped.count("\n"), stripped


def _emit_container_shell(
    node, span_node, chunks: list[Chunk], source: bytes, parent_symbol: str | None, spec: LanguageSpec, name: str | None
) -> None:
    first_def_start = _first_definition_start(node, spec.definition_types, spec.wrapper_types)
    end_byte = first_def_start if first_def_start is not None else node.end_byte
    trimmed = _trimmed_span(source, span_node.start_byte, end_byte)
    if trimmed is None:
        return
    end_line, text = trimmed
    chunks.append(
        Chunk(
            _line_number(source, span_node.start_byte),
            end_line,
            text,
            symbol_name=name,
            symbol_kind=_CONTAINER_KIND.get(node.type, "class"),
            parent_symbol=parent_symbol,
        )
    )


def _walk(
    node, spec: LanguageSpec, parent_symbol: str | None, chunks: list[Chunk], source: bytes, *, max_tokens: int, overlap_statements: int
) -> None:
    for child in node.children:
        if not child.is_named:
            continue
        span_node, inner_node = _unwrap(child, spec.wrapper_types)
        if inner_node.type in spec.definition_types:
            _emit_definition(
                inner_node, span_node, chunks, source, parent_symbol, max_tokens=max_tokens, overlap_statements=overlap_statements
            )
        elif inner_node.type in spec.container_types:
            name = _extract_name(inner_node)
            _emit_container_shell(inner_node, span_node, chunks, source, parent_symbol, spec, name)
            # Recurse into the real container node's children, not the
            # decorator wrapper (which has no children of its own besides
            # the decorator and the container).
            _walk(inner_node, spec, name, chunks, source, max_tokens=max_tokens, overlap_statements=overlap_statements)
        else:
            _walk(child, spec, parent_symbol, chunks, source, max_tokens=max_tokens, overlap_statements=overlap_statements)


def _emit_module_chunk(source: bytes, start_byte: int, end_byte: int, chunks: list[Chunk]) -> None:
    if end_byte <= start_byte:
        return
    trimmed = _trimmed_span(source, start_byte, end_byte)
    if trimmed is None:
        return
    end_line, text = trimmed
    chunks.append(
        Chunk(
            _line_number(source, start_byte),
            end_line,
            text,
            symbol_name=None,
            symbol_kind="module",
            parent_symbol=None,
        )
    )


def _chunk_ast(text: str, language: str, *, max_tokens: int, overlap_statements: int) -> list[Chunk] | None:
    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    source = text.encode("utf-8")
    tree = get_parser(language).parse(source)
    if tree.root_node.has_error:
        return None

    spec = LANGUAGE_SPECS[language]
    chunks: list[Chunk] = []
    _walk(tree.root_node, spec, None, chunks, source, max_tokens=max_tokens, overlap_statements=overlap_statements)

    # Module-level chunk(s) for whatever top-level code isn't covered by a
    # definition/container: not just leading/trailing, but every gap
    # between them too (a module constant between two functions, a
    # `export const foo = () => {...}` arrow-function export sitting next
    # to a `class`, ...). An earlier leading/trailing-only version of this
    # silently dropped anything in a middle gap — found by smoke-testing
    # against a real file, not by inspection.
    gap_start = 0
    for c in tree.root_node.children:
        if not c.is_named:
            continue
        span_node, inner_node = _unwrap(c, spec.wrapper_types)
        if inner_node.type in spec.definition_types or inner_node.type in spec.container_types:
            _emit_module_chunk(source, gap_start, span_node.start_byte, chunks)
            gap_start = span_node.end_byte
    _emit_module_chunk(source, gap_start, len(source), chunks)

    chunks.sort(key=lambda c: c.start_line)
    return chunks


# --- Markdown: heading-based chunking ---


def _chunk_markdown(text: str, *, max_tokens: int, naive_size: int, naive_overlap: int) -> list[Chunk]:
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    sections: list[tuple[int, list[str]]] = []  # (start_line, lines)
    current_start = 1
    current_lines: list[str] = []

    for i, line in enumerate(lines, start=1):
        if _HEADING_RE.match(line) and current_lines:
            sections.append((current_start, current_lines))
            current_start = i
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_start, current_lines))

    chunks: list[Chunk] = []
    max_chars = max_tokens * 4
    for start_line, section_lines in sections:
        content = "".join(section_lines)
        heading_match = _HEADING_RE.match(section_lines[0]) if section_lines else None
        heading_text = section_lines[0].strip().lstrip("#").strip() if heading_match else None

        if len(content) <= max_chars:
            end_line = start_line + len(section_lines) - 1
            chunks.append(
                Chunk(start_line, end_line, content, symbol_name=heading_text, symbol_kind="markdown_section", parent_symbol=None)
            )
            continue

        # Oversized section: fall back to char-windowing, offsetting line
        # numbers to the section's actual position in the file.
        for sub_start, sub_end, sub_content in naive_chunk_text(content, size=naive_size, overlap=naive_overlap):
            chunks.append(
                Chunk(
                    start_line + sub_start - 1,
                    start_line + sub_end - 1,
                    sub_content,
                    symbol_name=heading_text,
                    symbol_kind="markdown_section",
                    parent_symbol=None,
                )
            )
    return chunks
