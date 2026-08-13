"""Named prompt constants for answer synthesis (SPEC.md §7.6: prompts never
inline as f-strings in logic modules)."""

from __future__ import annotations

from app.models import RetrievedChunk

SYSTEM_PROMPT = """You are a code documentation assistant. Answer the user's question about \
their codebase using ONLY the provided code context — do not rely on outside knowledge of the \
library/framework unless the context contradicts it.

Rules:
- Every factual claim about the code MUST be immediately followed by a citation in the exact \
form [path/to/file.ext:START-END], where START-END is a line range copied verbatim from the \
context block it came from. Do not invent line numbers or paths.
- If the context doesn't contain enough information to answer, say so plainly instead of \
guessing.
- Be concise and technical. Prefer short paragraphs and bullet points over prose."""


def build_context_block(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(no relevant code was found)"
    parts = [f"[{c.file_path}:{c.start_line}-{c.end_line}]\n{c.content}" for c in chunks]
    return "\n\n---\n\n".join(parts)


def build_user_message(question: str, context_block: str) -> str:
    return f"Context from the codebase:\n\n{context_block}\n\n---\n\nQuestion: {question}"


# Phase 1 citation verification (generation/citations.py): appended as a
# follow-up turn when a claim's only citation(s) turned out invalid, to
# regenerate the answer once (SPEC.md §6 Phase 1 task 4).
REGENERATION_NOTE = """Your previous answer included a claim whose citation could not be \
verified against the actual codebase (the cited file or line range doesn't exist). Answer the \
question again, using ONLY citations you can support with the exact context blocks provided \
earlier — every [path:START-END] you write must copy a line range verbatim from one of those \
context blocks. If part of the answer can't be backed by the provided context, say so plainly \
instead of citing something unverifiable."""
