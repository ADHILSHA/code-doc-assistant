"""Named prompt constants for reranking (SPEC.md §7.6)."""

from __future__ import annotations

from app.models import RetrievedChunk

RERANK_SYSTEM_PROMPT = """You are ranking code snippets by relevance to a question. You will be shown \
a numbered list of candidate snippets and a question. Respond with ONLY a comma-separated list of \
snippet numbers, ordered from MOST to LEAST relevant — no other text, no explanation. Include only \
numbers for snippets that are at least somewhat relevant; omit any that are completely unrelated."""

_SNIPPET_CHARS = 300


def build_rerank_user_message(question: str, candidates: list[RetrievedChunk]) -> str:
    parts = [f"Question: {question}", "", "Candidates:"]
    for i, c in enumerate(candidates, start=1):
        label = f"[{i}] {c.file_path}:{c.start_line}-{c.end_line}"
        if c.symbol_name:
            label += f" ({c.symbol_name})"
        parts.append(f"{label}\n{c.content[:_SNIPPET_CHARS]}")
    return "\n\n".join(parts)
