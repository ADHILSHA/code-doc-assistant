from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from app.parsing.chunker import chunk_file, naive_chunk_text

# --- naive_chunk_text (Phase 0 splitter, still the designated fallback) ---


def test_naive_chunk_text_small_text_single_chunk():
    text = "line1\nline2\nline3\n"
    chunks = naive_chunk_text(text, size=1500, overlap=200)
    assert len(chunks) == 1
    start_line, end_line, content = chunks[0]
    assert start_line == 1
    assert end_line == text.count("\n")  # 3 newline-terminated lines, no trailing phantom line
    assert content == text


def test_naive_chunk_text_empty():
    assert naive_chunk_text("") == []


def test_naive_chunk_text_large_text_overlaps_and_covers_whole_file():
    text = "".join(f"line {i}\n" for i in range(1000))
    chunks = naive_chunk_text(text, size=1500, overlap=200)
    assert len(chunks) > 1
    for (_, e1, _), (s2, _, _) in pairwise(chunks):
        assert s2 <= e1
    assert chunks[-1][1] == text.count("\n")


# --- AST chunking: boundary correctness (Phase 1 acceptance criterion) ---

MINI_REPO = Path(__file__).resolve().parent / "fixtures" / "mini_repo"


def _assert_no_overlaps_and_in_bounds(chunks, total_lines: int) -> None:
    ordered = sorted(chunks, key=lambda c: c.start_line)
    for c in ordered:
        assert 1 <= c.start_line <= c.end_line <= total_lines, c
    for a, b in pairwise(ordered):
        # Oversized-function splits deliberately overlap by design (SPEC.md
        # §6 Phase 1 task 1: "1 statement of overlap") — only flag overlap
        # between chunks that *aren't* recognized split-siblings of the
        # same definition (same symbol_name/kind/parent).
        same_definition = (a.symbol_name, a.symbol_kind, a.parent_symbol) == (
            b.symbol_name,
            b.symbol_kind,
            b.parent_symbol,
        )
        if same_definition:
            continue
        assert b.start_line > a.end_line, f"overlap: {a} then {b}"


def test_chunk_service_py_boundaries_and_parent_symbol():
    text = (MINI_REPO / "src/users/service.py").read_text()
    result = chunk_file(text, "python")
    assert result.strategy == "ast"
    _assert_no_overlaps_and_in_bounds(result.chunks, text.count("\n") + 1)

    by_name = {c.symbol_name: c for c in result.chunks if c.symbol_name}
    assert by_name["get_user_by_id"].symbol_kind == "function"
    assert by_name["get_user_by_id"].parent_symbol == "UserService"
    assert by_name["UserService"].symbol_kind == "class"
    assert by_name["create_user"].parent_symbol == "UserService"

    # The method's own chunk must contain its full body, not spill into siblings.
    method_lines = by_name["get_user_by_id"].content.splitlines()
    assert "def get_user_by_id" in method_lines[0]
    assert not any("def create_user" in line for line in method_lines)


def test_chunk_models_py_no_imports_file():
    text = (MINI_REPO / "src/users/models.py").read_text()
    result = chunk_file(text, "python")
    assert result.strategy == "ast"
    _assert_no_overlaps_and_in_bounds(result.chunks, text.count("\n") + 1)
    names = {c.symbol_name for c in result.chunks}
    assert {"Address", "Profile", "format"} <= names


def test_chunk_typescript_module():
    text = (MINI_REPO / "web/userClient.ts").read_text()
    result = chunk_file(text, "typescript")
    assert result.strategy == "ast"
    _assert_no_overlaps_and_in_bounds(result.chunks, text.count("\n") + 1)

    by_name = {c.symbol_name: c for c in result.chunks if c.symbol_name}
    assert by_name["User"].symbol_kind == "interface"
    assert by_name["UserClient"].symbol_kind == "class"
    assert by_name["getUserById"].parent_symbol == "UserClient"
    assert by_name["createUser"].parent_symbol == "UserClient"


def test_chunk_oversized_function_splits_with_overlap():
    text = (MINI_REPO / "src/big_handler.py").read_text()
    result = chunk_file(text, "python", max_tokens=800, overlap_statements=1)
    assert result.strategy == "ast"

    handler_chunks = [c for c in result.chunks if c.symbol_name == "handle_big_request"]
    assert len(handler_chunks) > 1, "900-line function should split into multiple chunks"
    for c in handler_chunks:
        assert len(c.content) // 4 <= 800 * 1.2  # rough token estimate, allow slack for the header line

    # Split with 1 statement of overlap => consecutive windows' line ranges touch or overlap slightly.
    ordered = sorted(handler_chunks, key=lambda c: c.start_line)
    for a, b in pairwise(ordered):
        assert b.start_line <= a.end_line + 1


def test_chunk_markdown_heading_based():
    text = (MINI_REPO / "README.md").read_text()
    result = chunk_file(text, "markdown")
    assert result.strategy == "markdown"
    assert all(c.symbol_kind == "markdown_section" for c in result.chunks)
    headings = [c.symbol_name for c in result.chunks]
    assert "mini_repo" in headings


def test_chunk_unsupported_language_falls_back_to_naive():
    result = chunk_file("public class Foo {}\n", "php")
    assert result.strategy == "naive"
    assert len(result.chunks) == 1


def test_chunk_broken_syntax_falls_back_to_naive():
    result = chunk_file("def foo(:\n    not valid python at all ][", "python")
    assert result.strategy == "naive"


def test_chunk_empty_file():
    assert chunk_file("", "python").chunks == []
    assert chunk_file("", None).chunks == []


def test_chunk_all_mini_repo_source_files_have_no_overlaps():
    """Broad sweep: every real supported-language file in the fixture repo
    should chunk with no overlapping/out-of-bounds ranges, regardless of
    which language-specific code path it exercises."""
    targets = [
        ("src/users/service.py", "python"),
        ("src/users/models.py", "python"),
        ("src/auth/auth.py", "python"),
        ("src/big_handler.py", "python"),
        ("web/userClient.ts", "typescript"),
        ("web/index.ts", "typescript"),
    ]
    for rel_path, language in targets:
        text = (MINI_REPO / rel_path).read_text()
        result = chunk_file(text, language)
        _assert_no_overlaps_and_in_bounds(result.chunks, text.count("\n") + 1)
