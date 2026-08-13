"""Extension → language mapping and tree-sitter grammar selection.

Supersedes `walker.py`'s Phase 0 placeholder extension map (see
DECISIONS.md) — this is now the single source of truth for both
`files.language` detection and which tree-sitter grammar `chunker.py` uses.
"""

from __future__ import annotations

from pathlib import Path

# SPEC.md §6 Phase 1 task 1: "Support at minimum: Python, JavaScript,
# TypeScript, TSX, Go, Java, Rust, Ruby." These names must match
# `tree_sitter_language_pack.get_parser()`'s expected language identifiers —
# verified directly against the installed package before writing this.
TREE_SITTER_LANGUAGES = {
    "python",
    "javascript",
    "typescript",
    "tsx",
    "go",
    "java",
    "rust",
    "ruby",
}

# Markdown is chunked separately (heading-based text splitting — see
# parsing/chunker.py) rather than via a tree-sitter grammar.
MARKDOWN_LANGUAGES = {"markdown"}

_EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".md": "markdown",
    ".markdown": "markdown",
}


def detect_language(path: str) -> str | None:
    """Internal language name for a file path (by extension), or None if
    unrecognized. Independent of whether that language has a tree-sitter
    grammar wired up — see `has_tree_sitter_grammar`."""
    return _EXTENSION_LANGUAGE.get(Path(path).suffix.lower())


def has_tree_sitter_grammar(language: str | None) -> bool:
    return language in TREE_SITTER_LANGUAGES


def is_markdown(language: str | None) -> bool:
    return language in MARKDOWN_LANGUAGES
