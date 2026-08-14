"""Agent tool implementations (SPEC.md §6 Phase 3 task 3).

Each tool is a plain function `(ctx: ToolContext, args: dict) -> (result,
summary)` — `result` is whatever JSON-serializable payload gets sent back
to the model as the tool_result; `summary` is the short one-line string the
`tool` SSE event's `result_summary` field shows the UI (SPEC.md §5), so the
user sees "12 matches in 5 files" without the full payload round-tripping
through the event stream.

`read_file`/`grep`/`find_files` are the only tools that touch the repo's
working tree on disk — all path-jailed to `ctx.repo_root` via
ingest/safe_path.py, the same guard api/browse.py's `GET /file` uses
(SPEC.md §7.5, and this phase's explicit "path traversal attempts are
rejected" acceptance criterion). Every other tool reads only from the
per-repo SQLite DB.

`execute_tool` is the single entry point agent/loop.py calls — it never
lets a tool's exception escape: a broken tool call becomes a visible error
result the model can see and route around, not a crashed agent loop.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ingest.redact import redact_secrets
from app.ingest.safe_path import PathTraversalError, safe_join
from app.providers.embeddings import EmbeddingProvider
from app.retrieval import structured
from app.retrieval.chunks import fetch_chunks
from app.retrieval.hybrid import hybrid_search

_GREP_TIMEOUT_SECONDS = 10
_GREP_LINE_CHARS = 200


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    repo_root: Path
    embedding_provider: EmbeddingProvider
    settings: Settings


ToolFn = Callable[[ToolContext, dict[str, Any]], tuple[Any, str]]


def execute_tool(name: str, ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}, f"unknown tool {name!r}"
    try:
        return handler(ctx, args)
    except Exception as exc:  # noqa: BLE001 - a tool failure must surface to the model, not crash the loop
        return {"error": str(exc)}, f"tool {name!r} failed: {exc}"


def tool_semantic_search(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    query = str(args["query"])
    k = int(args.get("k") or 10)
    ranked = hybrid_search(
        ctx.conn, ctx.embedding_provider, query,
        top_k_dense=ctx.settings.top_k_dense, top_k_lexical=ctx.settings.top_k_lexical,
        rrf_k=ctx.settings.rrf_k, top_n=k,
    )
    chunks = fetch_chunks(ctx.conn, [cid for cid, _score in ranked])
    result = [
        {"path": c.file_path, "start_line": c.start_line, "end_line": c.end_line,
         "symbol": c.symbol_name, "snippet": c.content[:300]}
        for c in chunks
    ]
    return result, f"{len(result)} result(s) for {query!r}"


def tool_grep(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    pattern = str(args["pattern"])
    glob = args.get("glob")
    max_results = min(int(args.get("max_results") or ctx.settings.agent_grep_max_results),
                       ctx.settings.agent_grep_max_results)
    matches = _grep_ripgrep(ctx, pattern, glob, max_results)
    if matches is None:
        matches = _grep_python_fallback(ctx, pattern, glob, max_results)
    return matches, f"{len(matches)} match(es) for {pattern!r}"


def _grep_ripgrep(ctx: ToolContext, pattern: str, glob: str | None, max_results: int) -> list[dict] | None:
    if shutil.which("rg") is None:
        return None
    cmd = ["rg", "--line-number", "--no-heading", "--color=never"]
    if glob:
        cmd += ["--glob", glob]
    cmd += ["--", pattern, "."]
    try:
        proc = subprocess.run(
            cmd, cwd=ctx.repo_root, capture_output=True, text=True,
            timeout=_GREP_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):  # 1 = ripgrep ran fine, just no matches
        return None
    matches: list[dict] = []
    for line in proc.stdout.splitlines():
        path, sep, rest = line.partition(":")
        lineno_str, sep2, text = rest.partition(":")
        if not (sep and sep2 and lineno_str.isdigit()):
            continue
        matches.append(
            {"path": path.removeprefix("./"), "line": int(lineno_str), "text": redact_secrets(text.strip()[:_GREP_LINE_CHARS])}
        )
        if len(matches) >= max_results:
            break
    return matches


def _grep_python_fallback(ctx: ToolContext, pattern: str, glob: str | None, max_results: int) -> list[dict]:
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))

    matches: list[dict] = []
    paths = [r["path"] for r in ctx.conn.execute("SELECT path FROM files ORDER BY path")]
    for path in paths:
        if glob and not fnmatch.fnmatch(path, glob):
            continue
        try:
            text = safe_join(ctx.repo_root, path).read_text(encoding="utf-8", errors="replace")
        except (PathTraversalError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append({"path": path, "line": lineno, "text": redact_secrets(line.strip()[:_GREP_LINE_CHARS])})
                if len(matches) >= max_results:
                    return matches
    return matches


def tool_find_files(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    glob = str(args["glob"])
    paths = [r["path"] for r in ctx.conn.execute("SELECT path FROM files ORDER BY path")]
    matched = [p for p in paths if fnmatch.fnmatch(p, glob)][:200]
    return matched, f"{len(matched)} file(s) matching {glob!r}"


def tool_read_file(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    path = str(args["path"])
    try:
        abs_path = safe_join(ctx.repo_root, path)
    except PathTraversalError as exc:
        return {"error": str(exc)}, "rejected: path escapes the repo root"

    if not abs_path.is_file():
        return {"error": f"file not found: {path}"}, "file not found"

    all_lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(all_lines)
    if total == 0:
        return {"path": path, "start_line": 0, "end_line": 0, "lines": []}, "file is empty"

    start_line = max(1, min(int(args.get("start_line") or 1), total))
    requested_end = int(args["end_line"]) if args.get("end_line") else total
    max_end = start_line + ctx.settings.agent_read_file_max_lines - 1
    end_line = max(start_line, min(total, requested_end, max_end))

    # SPEC.md §6 Phase 5 task 3 / §7.5: read_file bypasses the (already-
    # redacted, see index/store.py) chunk store entirely — it reads live
    # from disk, the same as grep — so this is the redaction boundary for
    # this tool, applied right before the result becomes a tool_result sent
    # to the model.
    selected_lines = [redact_secrets(line) for line in all_lines[start_line - 1 : end_line]]
    result = {"path": path, "start_line": start_line, "end_line": end_line, "lines": selected_lines}
    return result, f"{path}:{start_line}-{end_line}"


def tool_get_definition(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    name = str(args["symbol"])
    defs = structured.get_definition(ctx.conn, name)
    return [asdict(d) for d in defs], f"{len(defs)} definition(s) for {name!r}"


def tool_find_references(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    name = str(args["symbol"])
    refs = structured.find_references(ctx.conn, name)
    return [asdict(r) for r in refs], f"{len(refs)} reference(s) to {name!r}"


def tool_list_directory(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    path = str(args.get("path") or "")
    listing = structured.list_directory_contents(ctx.conn, path)
    return asdict(listing), f"{len(listing.directories)} dir(s), {len(listing.files)} file(s) in {path or '.'}"


def tool_get_dependencies(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    deps = structured.list_dependencies(ctx.conn)
    return [asdict(d) for d in deps], f"{len(deps)} dependenc{'y' if len(deps) == 1 else 'ies'}"


def tool_list_endpoints(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    endpoints = structured.list_endpoints(ctx.conn)
    return [asdict(e) for e in endpoints], f"{len(endpoints)} endpoint(s)"


def tool_get_summary(ctx: ToolContext, args: dict[str, Any]) -> tuple[Any, str]:
    path = str(args.get("path") or ".")
    row = ctx.conn.execute(
        "SELECT scope, content FROM summaries WHERE target_path = ? "
        "ORDER BY CASE scope WHEN 'file' THEN 0 WHEN 'directory' THEN 1 ELSE 2 END LIMIT 1",
        (path,),
    ).fetchone()
    if row is None:
        return {"error": f"no summary available for {path!r}"}, "no summary found"
    return {"scope": row["scope"], "target_path": path, "content": row["content"]}, f"{row['scope']} summary for {path or '.'}"


TOOL_HANDLERS: dict[str, ToolFn] = {
    "semantic_search": tool_semantic_search,
    "grep": tool_grep,
    "find_files": tool_find_files,
    "read_file": tool_read_file,
    "get_definition": tool_get_definition,
    "find_references": tool_find_references,
    "list_directory": tool_list_directory,
    "get_dependencies": tool_get_dependencies,
    "list_endpoints": tool_list_endpoints,
    "get_summary": tool_get_summary,
}

# Anthropic tool-use schema shape (`name`/`description`/`input_schema` as
# plain JSON Schema) — this is the one place the tool *definitions* are
# vendor-shaped rather than the request-shape compromise noted in
# providers/llm.py; a second provider would need its own translation of
# this list, not a rewrite of the tools themselves.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "semantic_search",
        "description": "Semantic (embedding + lexical) search over the codebase for chunks relevant to a natural-language query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "description": "max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "grep",
        "description": "Search the repo's working tree for a regex pattern, optionally filtered by filename glob.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string", "description": "optional filename glob filter, e.g. '*.py'"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find_files",
        "description": "Find indexed file paths matching a glob pattern, e.g. '**/*.py'.",
        "input_schema": {"type": "object", "properties": {"glob": {"type": "string"}}, "required": ["glob"]},
    },
    {
        "name": "read_file",
        "description": "Read a slice of a file's lines (capped per call — request a narrower range and call again for more).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_definition",
        "description": "Look up where a symbol (class/function/method name) is defined.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "find_references",
        "description": "Find every resolved call site that references a given symbol name.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "list_directory",
        "description": 'List the immediate files and subdirectories of a directory ("" for the repo root).',
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {
        "name": "get_dependencies",
        "description": "List every dependency declared in the repo's manifest file(s).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_endpoints",
        "description": "List every API endpoint detected in the repo.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_summary",
        "description": 'Get the cached hierarchical summary for a file or directory path (path="." for the whole repo).',
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
]
