"""GET /repos/{id}/file — backs the frontend CodeViewer (SPEC.md §5, §6
Phase 1 task 5). Reads directly from the repo's cloned/local working tree
on disk (not from the chunk index), path-jailed to the repo root.

No business logic here beyond request validation and error mapping — the
path-jailing itself lives in ingest/safe_path.py (SPEC.md §7.6).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.repos import registry_connection_dependency
from app.ingest.safe_path import PathTraversalError, safe_join
from app.models import FileSliceResponse
from app.parsing.languages import detect_language

router = APIRouter(tags=["browse"])


@router.get("/repos/{repo_id}/file", response_model=FileSliceResponse)
def get_file_slice(
    repo_id: str,
    path: str,
    start: int | None = Query(default=None, ge=1),
    end: int | None = Query(default=None, ge=1),
    conn: sqlite3.Connection = Depends(registry_connection_dependency),
) -> FileSliceResponse:
    row = conn.execute("SELECT local_path FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "repo not found")
    if not row["local_path"]:
        raise HTTPException(409, "repo source has not been resolved yet")

    repo_root = Path(row["local_path"])
    try:
        abs_path = safe_join(repo_root, path)
    except PathTraversalError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not abs_path.is_file():
        raise HTTPException(404, f"file not found: {path}")

    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(500, f"could not read file: {exc}") from exc

    all_lines = text.splitlines()
    total = len(all_lines)
    if total == 0:
        raise HTTPException(404, f"file is empty: {path}")

    start_line = max(1, start or 1)
    if start_line > total:
        raise HTTPException(400, f"start line {start_line} is beyond the file's {total} lines")
    end_line = max(start_line, min(total, end or total))

    return FileSliceResponse(
        path=path,
        language=detect_language(path),
        start_line=start_line,
        end_line=end_line,
        lines=all_lines[start_line - 1 : end_line],
    )
