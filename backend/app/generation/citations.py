"""Parse + VERIFY citations (SPEC.md §6 Phase 1 task 4).

The synthesis prompt requires citations as `[path/to/file.py:120-145]`.
Every citation is checked here against real file/line data before an
answer is returned: the path must exist in `files`, and the line range
must be within `[1, files.loc]`. Invalid citations are stripped and logged
as `hallucinated_citation`. If a claim's *only* citation(s) were all
invalid, `VerificationResult.unsupported_claim` tells the caller
(generation/answer.py) to regenerate the answer once, noting the failure —
this module only verifies; it doesn't call the LLM itself.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from app.models import Citation

logger = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\[([\w./-]+):(\d+)-(\d+)\]")
# Claim boundaries: split on sentence-ending punctuation or blank lines.
# Approximate (not real NLP sentence segmentation) — good enough to decide
# whether an invalid citation left its claim wholly unsupported.
_CLAIM_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class VerificationResult:
    citations: list[Citation]  # only the valid ones, sequentially re-numbered
    invalid_count: int
    unsupported_claim: bool  # some claim's only citation(s) were all invalid


@dataclass(frozen=True)
class RepoContext:
    """Just enough repo metadata to build a GitHub permalink."""

    source_type: str  # "github" | "local"
    owner_repo: str | None  # "owner/name", only set for source_type == "github"
    commit_sha: str | None


def _file_bounds(conn: sqlite3.Connection) -> dict[str, int]:
    """path -> loc, computed once per verification instead of one query per
    citation."""
    return {r["path"]: r["loc"] or 0 for r in conn.execute("SELECT path, loc FROM files")}


def _is_valid(path: str, start: int, end: int, bounds: dict[str, int]) -> bool:
    loc = bounds.get(path)
    if loc is None:
        return False
    return 1 <= start <= end <= loc


def _claim_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for m in _CLAIM_BOUNDARY_RE.finditer(text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return [s for s in spans if s[1] > s[0]]


def verify_citations(conn: sqlite3.Connection, answer_text: str) -> VerificationResult:
    bounds = _file_bounds(conn)
    raw = [
        (m.group(1), int(m.group(2)), int(m.group(3)), m.start())
        for m in _CITATION_RE.finditer(answer_text)
    ]

    valid: list[Citation] = []
    invalid_count = 0
    unsupported_claim = False

    for claim_start, claim_end in _claim_spans(answer_text):
        claim_citations = [c for c in raw if claim_start <= c[3] < claim_end]
        if not claim_citations:
            continue
        any_valid_in_claim = False
        for path, start_line, end_line, _pos in claim_citations:
            if _is_valid(path, start_line, end_line, bounds):
                any_valid_in_claim = True
                valid.append(Citation(id=len(valid) + 1, path=path, start_line=start_line, end_line=end_line))
            else:
                invalid_count += 1
                logger.warning(
                    "hallucinated_citation: path=%r lines=%d-%d (not found or out of bounds)",
                    path,
                    start_line,
                    end_line,
                )
        if not any_valid_in_claim:
            unsupported_claim = True

    return VerificationResult(citations=valid, invalid_count=invalid_count, unsupported_claim=unsupported_claim)


def build_permalink(repo: RepoContext, path: str, start_line: int, end_line: int) -> str | None:
    if repo.source_type != "github" or not repo.owner_repo or not repo.commit_sha:
        return None
    frag = f"L{start_line}" if start_line == end_line else f"L{start_line}-L{end_line}"
    return f"https://github.com/{repo.owner_repo}/blob/{repo.commit_sha}/{path}#{frag}"


def attach_permalinks(citations: list[Citation], repo: RepoContext) -> list[Citation]:
    return [
        Citation(
            id=c.id,
            path=c.path,
            start_line=c.start_line,
            end_line=c.end_line,
            url=build_permalink(repo, c.path, c.start_line, c.end_line),
        )
        for c in citations
    ]
