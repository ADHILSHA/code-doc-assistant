"""Structured (SQL, not LLM) answers for the `dependencies`, `endpoints`,
and `locate` routes (SPEC.md §6 Phase 2 task 5) — exact, deterministic,
answer in well under a second, and can't hallucinate since every field
comes straight out of a table that's a direct product of parsing (Phase 2
tasks 1-4), not a generated summary of it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolDefinition:
    name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    parent_symbol: str | None


def get_definition(conn: sqlite3.Connection, name: str) -> list[SymbolDefinition]:
    """Every symbol named `name` — there can be more than one (same name in
    different files, an overridden method, ...). [] if nothing matches.
    Ordered exported-first then by path, so the most likely "real"
    definition sorts first without silently hiding the rest."""
    rows = conn.execute(
        "SELECT s.name, s.kind, f.path, s.start_line, s.end_line, s.signature, s.docstring, "
        "s.parent_symbol "
        "FROM symbols s JOIN files f ON f.id = s.file_id "
        "WHERE s.name = ? ORDER BY s.is_exported DESC, f.path, s.start_line",
        (name,),
    ).fetchall()
    return [
        SymbolDefinition(
            name=r["name"], kind=r["kind"], file_path=r["path"], start_line=r["start_line"],
            end_line=r["end_line"], signature=r["signature"], docstring=r["docstring"],
            parent_symbol=r["parent_symbol"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class EndpointRow:
    method: str | None
    route: str
    framework: str | None
    handler_symbol: str | None
    file_path: str | None
    line: int | None
    auth_hint: str | None


def list_endpoints(conn: sqlite3.Connection) -> list[EndpointRow]:
    rows = conn.execute(
        "SELECT e.method, e.route, e.framework, e.handler_symbol, f.path, e.line, e.auth_hint "
        "FROM endpoints e LEFT JOIN files f ON f.id = e.file_id "
        "ORDER BY e.route, e.method"
    ).fetchall()
    return [
        EndpointRow(
            method=r["method"], route=r["route"], framework=r["framework"],
            handler_symbol=r["handler_symbol"], file_path=r["path"], line=r["line"],
            auth_hint=r["auth_hint"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class DependencyRow:
    ecosystem: str | None
    name: str
    version_spec: str | None
    kind: str | None
    manifest_path: str


def list_dependencies(conn: sqlite3.Connection) -> list[DependencyRow]:
    rows = conn.execute(
        "SELECT ecosystem, name, version_spec, kind, manifest_path FROM dependencies "
        "ORDER BY ecosystem, name"
    ).fetchall()
    return [
        DependencyRow(
            ecosystem=r["ecosystem"], name=r["name"], version_spec=r["version_spec"],
            kind=r["kind"], manifest_path=r["manifest_path"],
        )
        for r in rows
    ]


# --- "locate" route: pull a candidate symbol name out of a free-text question ---

_STOPWORDS = {
    "where", "is", "are", "does", "do", "the", "a", "an", "defined", "define",
    "implemented", "located", "file", "files", "which", "handles", "handle",
    "module", "function", "class", "method", "for", "in", "of", "this", "code",
    "codebase", "repo", "project", "find", "locate", "what", "and", "or", "to",
}
# Backtick-quoted identifiers are the strongest signal ("Where is `UserService`
# defined?"); bare identifiers are a fallback, filtered against _STOPWORDS and
# preferring ones that look like a real symbol name (CamelCase or snake_case
# with an underscore) over an ordinary lowercase English word we didn't think
# to stop-list.
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_LOOKS_LIKE_SYMBOL_RE = re.compile(r"^[A-Z][a-zA-Z0-9]*[A-Z]|_")


def extract_candidate_names(question: str) -> list[str]:
    """Best-effort, ordered by confidence. Callers should try each in turn
    against `get_definition` and fall back to hybrid retrieval if none
    match — a bad guess here should degrade gracefully, not produce a wrong
    "confident" answer."""
    backticked = _BACKTICK_RE.findall(question)

    bare = [
        w for w in _IDENT_RE.findall(question)
        if w.lower() not in _STOPWORDS and w not in backticked
    ]
    symbol_like = [w for w in bare if _LOOKS_LIKE_SYMBOL_RE.search(w)]
    other = [w for w in bare if w not in symbol_like]

    seen: set[str] = set()
    ordered: list[str] = []
    for name in [*backticked, *symbol_like, *other]:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered
