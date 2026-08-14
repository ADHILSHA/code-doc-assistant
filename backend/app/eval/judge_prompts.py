"""Named prompt constants for LLM-as-judge answer-correctness scoring
(SPEC.md §7.6)."""

from __future__ import annotations

from app.eval.golden import GoldenQuestion

JUDGE_SYSTEM_PROMPT = """You are grading an AI code-documentation assistant's answer against a \
golden reference for a question about a codebase. Score the answer 0, 1, or 2:
  0 = wrong, missing the key facts, or contradicts the reference
  1 = partially correct — gets the general idea but misses or gets a specific detail wrong
  2 = fully correct — covers everything the reference expects
Respond with ONLY the digit (0, 1, or 2) — no explanation, no punctuation."""


def build_judge_user_message(question: GoldenQuestion, answer_text: str) -> str:
    parts = [f"Question: {question.question}"]
    if question.expected_substrings:
        parts.append(
            "The answer should mention (case-insensitive, doesn't need to be verbatim): "
            + ", ".join(question.expected_substrings)
        )
    if question.notes:
        parts.append(f"Grading notes: {question.notes}")
    parts.append(f"\nActual answer:\n{answer_text}")
    return "\n".join(parts)
