"""Reranking (SPEC.md §6 Phase 3 task 2): cheap-LLM relevance scoring of
the hybrid-search-plus-expansion candidate set, keeping the top `keep_n`;
then merging adjacent same-file chunks into contiguous spans so the
synthesis prompt sees fewer, longer blocks instead of many small fragments.

LLM-based, not a cross-encoder: SPEC.md offers "cross-encoder or LLM-based"
as alternatives, and a cross-encoder means a new heavy model dependency
(sentence-transformers + a downloaded model) purely for this one step,
duplicating the provider-abstraction problem the project already solved
for embeddings/LLMs. Reuses whatever `LLMProvider` the caller passes in —
generation/answer.py passes the cheap summarization provider
(`get_summarization_llm_provider`), the same one retrieval/router.py's LLM
fallback uses.
"""

from __future__ import annotations

import re

from app.models import RetrievedChunk
from app.providers.llm import LLMProvider
from app.retrieval.rerank_prompts import RERANK_SYSTEM_PROMPT, build_rerank_user_message

# How close two same-file chunks' line ranges can be and still count as
# "adjacent" for merge_adjacent — 0 would only merge back-to-back chunks
# with zero gap; a couple of lines of slack also catches the common case
# of two sibling top-level definitions separated by a blank line or two.
_MERGE_MAX_GAP_LINES = 2

_NUMBER_RE = re.compile(r"\d+")


def rerank(
    llm_provider: LLMProvider, question: str, candidates: list[RetrievedChunk], *, keep_n: int
) -> list[RetrievedChunk]:
    """Best-first `candidates`, reordered by LLM-judged relevance and cut
    to `keep_n`. Already-short lists pass through untouched (asking an LLM
    to rank 5 candidates down to 12 is pointless spend). Fails open to
    "keep the incoming order, truncate to keep_n" if the LLM response can't
    be parsed into a valid ranking — a reranking hiccup shouldn't take down
    the whole answer.
    """
    if len(candidates) <= keep_n:
        return list(candidates)

    response_text = llm_provider.complete(
        system=RERANK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_rerank_user_message(question, candidates)}],
        max_tokens=200,
    ).text
    order = _parse_ranking(response_text, len(candidates))
    if order is None:
        return candidates[:keep_n]
    return [candidates[i] for i in order[:keep_n]]


def _parse_ranking(text: str, n: int) -> list[int] | None:
    """1-indexed snippet numbers from the LLM's response -> 0-indexed,
    deduplicated, in-range. None if nothing usable came back at all."""
    order: list[int] = []
    seen: set[int] = set()
    for match in _NUMBER_RE.finditer(text):
        idx = int(match.group()) - 1
        if 0 <= idx < n and idx not in seen:
            seen.add(idx)
            order.append(idx)
    return order or None


def merge_adjacent(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Order-preserving-per-file merge: groups by `file_path` (each file's
    group ordered by wherever its best-ranked chunk first appeared in
    `chunks`), sorts each group by `start_line`, and concatenates the
    content of any two chunks whose line ranges are adjacent or overlapping
    (gap <= `_MERGE_MAX_GAP_LINES`) into one span.

    The merged span's line range is always exact (min start, max end) even
    though its concatenated `content` may not perfectly reproduce a small
    gap between two merged chunks verbatim — harmless for the synthesis
    prompt (a little missing filler text at worst), and citation *validity*
    only depends on the line range being real (generation/citations.py
    checks bounds, not exact content), so this can never produce an
    invalid citation.
    """
    by_file: dict[str, list[RetrievedChunk]] = {}
    file_order: list[str] = []
    for c in chunks:
        if c.file_path not in by_file:
            by_file[c.file_path] = []
            file_order.append(c.file_path)
        by_file[c.file_path].append(c)

    merged: list[RetrievedChunk] = []
    for path in file_order:
        group = sorted(by_file[path], key=lambda c: c.start_line)
        current = group[0]
        for nxt in group[1:]:
            if nxt.start_line <= current.end_line + _MERGE_MAX_GAP_LINES:
                current = current.model_copy(
                    update={
                        "end_line": max(current.end_line, nxt.end_line),
                        "content": current.content + "\n" + nxt.content,
                        "symbol_name": current.symbol_name or nxt.symbol_name,
                    }
                )
            else:
                merged.append(current)
                current = nxt
        merged.append(current)
    return merged
