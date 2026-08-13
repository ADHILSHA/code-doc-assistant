"""Named prompt constants for the agent loop (SPEC.md §7.6)."""

from __future__ import annotations

from app.models import RetrievedChunk

AGENT_SYSTEM_PROMPT = """You are a code documentation assistant investigating a question about a \
codebase using tools. You have already been given an initial set of relevant code chunks below — \
use them as a starting point, and call tools to find whatever else you need (definitions, callers, \
other files, directory structure, dependencies, endpoints, summaries) to fully answer multi-hop \
questions that span more than one file.

Rules:
- Every factual claim about the code MUST be immediately followed by a citation in the exact form \
[path/to/file.ext:START-END], where START-END is a line range you actually saw (from the initial \
context or a tool result) — never invent a line number or path.
- Prefer the most targeted tool for the question: get_definition/find_references for "where is X" \
questions, semantic_search for conceptual questions, grep for exact-string questions, read_file to \
see more of a file you already found.
- When you have enough information, stop calling tools and write the final answer directly (no tool \
call) — don't call tools just to double-check something you already confirmed.
- If you reach the end of your tool budget before you're fully confident, answer with what you've \
gathered and say plainly what you weren't able to verify, rather than guessing."""


def build_agent_seed_message(question: str, seed_chunks: list[RetrievedChunk]) -> str:
    """SPEC.md Phase 3 task 3: "Seed the agent with the phase-2 hybrid
    retrieval results so it never starts blind" — this is that seed,
    formatted the same way generation/prompts.py's build_context_block
    formats context for the non-agentic path, so the model sees a
    consistent citation-source shape either way."""
    if not seed_chunks:
        context_block = "(no chunks matched this question directly — use the tools to investigate)"
    else:
        parts = [f"[{c.file_path}:{c.start_line}-{c.end_line}]\n{c.content}" for c in seed_chunks]
        context_block = "\n\n---\n\n".join(parts)
    return f"Initial relevant code (from hybrid search):\n\n{context_block}\n\n---\n\nQuestion: {question}"


BUDGET_EXHAUSTED_NOTE = """You've used your full tool-call budget for this question. Based on \
everything you've found so far, write your final answer now — don't call any more tools. If some \
part of the question remains unresolved, say so plainly instead of guessing."""
