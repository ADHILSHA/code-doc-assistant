"""Call/reference extraction and import edges (SPEC.md §6 Phase 2 task 2).

Two independent extractions, both AST-based:
- `extract_calls`: every call-expression in a file, as (target_name, line).
  Resolution against the repo-wide `symbols` table (by name, accepting
  ambiguity — SPEC.md explicitly allows this) happens at the call site in
  index/store.py, not here; this module only extracts, it doesn't touch
  the DB.
- `extract_imports`: every import/require statement, as (module_text,
  line). Resolving a relative import to a real `files` row is done here
  (`resolve_import`) for Python and JS/TS/TSX, where the on-disk file
  layout directly determines the module path. Go/Java/Rust/Ruby imports
  are extracted but always reported unresolved/external — their import
  conventions need classpath/crate/gem knowledge this project doesn't
  model, and no Phase 2 acceptance criterion depends on resolving them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.parsing.languages import has_tree_sitter_grammar

# --- call expressions ---

# (call node type, field name holding the callee expression)
_CALL_SPECS: dict[str, tuple[str, str]] = {
    "python": ("call", "function"),
    "javascript": ("call_expression", "function"),
    "typescript": ("call_expression", "function"),
    "tsx": ("call_expression", "function"),
    "go": ("call_expression", "function"),
    "java": ("method_invocation", "name"),
    "rust": ("call_expression", "function"),
    "ruby": ("call", "method"),
}


@dataclass(frozen=True)
class CallRef:
    target_name: str
    line: int


def extract_calls(text: str, language: str | None) -> list[CallRef]:
    if not has_tree_sitter_grammar(language) or language not in _CALL_SPECS:
        return []
    assert language is not None

    from tree_sitter_language_pack import get_parser  # lazy import: heavy optional dependency

    source = text.encode("utf-8")
    tree = get_parser(language).parse(source)
    if tree.root_node.has_error:
        return []

    node_type, field_name = _CALL_SPECS[language]
    calls: list[CallRef] = []
    _find_calls(tree.root_node, node_type, field_name, source, calls)
    return calls


def _find_calls(node, node_type: str, field_name: str, source: bytes, out: list[CallRef]) -> None:
    if node.type == node_type:
        callee = node.child_by_field_name(field_name)
        if callee is not None:
            text = callee.text.decode("utf-8", errors="replace")
            name = text.rsplit(".", 1)[-1]  # `self.bar` / `obj.method` -> `bar` / `method`
            if name.isidentifier():
                line = source.count(b"\n", 0, node.start_byte) + 1
                out.append(CallRef(target_name=name, line=line))
    for child in node.children:
        _find_calls(child, node_type, field_name, source, out)


# --- imports ---

_IMPORT_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^(?:from\s+(\S+)\s+import|import\s+(\S+))", re.MULTILINE),
    "javascript": re.compile(r"""from\s+["'](\S+?)["']|require\(\s*["'](\S+?)["']\s*\)"""),
    "typescript": re.compile(r"""from\s+["'](\S+?)["']|require\(\s*["'](\S+?)["']\s*\)"""),
    "tsx": re.compile(r"""from\s+["'](\S+?)["']|require\(\s*["'](\S+?)["']\s*\)"""),
    "go": re.compile(r'^\s*(?:import\s+)?"([\w./-]+)"\s*$', re.MULTILINE),
    "java": re.compile(r"^import\s+(?:static\s+)?([\w.]+);", re.MULTILINE),
    "rust": re.compile(r"^use\s+([\w:]+)", re.MULTILINE),
    "ruby": re.compile(r"""require(?:_relative)?\s*["'](\S+?)["']"""),
}

_RESOLVABLE_LANGUAGES = {"python", "javascript", "typescript", "tsx"}

_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx")


@dataclass(frozen=True)
class ImportRef:
    module_text: str
    line: int


def extract_imports(text: str, language: str | None) -> list[ImportRef]:
    """Every import/require statement in the file, unlike
    parsing/imports.py's header-enrichment helper this isn't capped or
    deduplicated — SPEC.md wants each one recorded as its own
    `import_edges` row."""
    pattern = _IMPORT_PATTERNS.get(language or "")
    if pattern is None:
        return []
    refs: list[ImportRef] = []
    for match in pattern.finditer(text):
        name = next((g for g in match.groups() if g), None)
        if name:
            line = text.count("\n", 0, match.start()) + 1
            refs.append(ImportRef(module_text=name, line=line))
    return refs


def resolve_import(
    module_text: str, *, from_path: str, language: str | None, known_paths: set[str]
) -> tuple[str | None, bool]:
    """Returns (resolved_path_or_None, is_external). `known_paths` is the
    set of every `files.path` already indexed for this repo."""
    if language not in _RESOLVABLE_LANGUAGES:
        return None, True

    if language == "python":
        if not module_text.startswith("."):
            # A same-package absolute-looking import (`src.auth.auth`) is
            # only really "internal" if it resolves to a file we indexed;
            # anything else (stdlib, a third-party package) is external.
            candidate = module_text.replace(".", "/")
            module_path = candidate + ".py"
            if module_path in known_paths:
                return module_path, False
            init_path = candidate + "/__init__.py"
            if init_path in known_paths:
                return init_path, False
        return None, True

    # JS/TS/TSX: only "./" and "../" specifiers are ever internal — a bare
    # specifier (`"express"`, `"react"`) is a package import.
    if not module_text.startswith("."):
        return None, True

    base_dir = PurePosixPath(from_path).parent
    resolved_base = (base_dir / module_text).as_posix()
    # normalize `a/b/../c` -> `a/c` without touching the filesystem
    normalized = PurePosixPath(resolved_base)
    parts: list[str] = []
    for part in normalized.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    resolved_base = "/".join(parts)

    candidates = [resolved_base + ext for ext in _JS_EXTENSIONS]
    candidates += [resolved_base + "/index" + ext for ext in _JS_EXTENSIONS]
    if resolved_base in known_paths:
        candidates.insert(0, resolved_base)
    for candidate in candidates:
        if candidate in known_paths:
            return candidate, False
    return None, True
