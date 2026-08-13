"""Agentic tool-use loop (SPEC.md §6 Phase 3 task 3): for multi-hop
questions, seeds the model with Phase 2's hybrid retrieval results (so it
"never starts blind"), then lets it call tools (agent/tools.py) across up
to `max_iterations` turns, `max_context_tokens` of accumulated context, and
`max_wall_seconds` of wall-clock time — whichever comes first. On
exhaustion, one final turn asks the model to answer with whatever it has
gathered rather than silently truncating mid-thought.

`run_agent` is a plain sync generator, same shape as generation/answer.py's
own SSE-event generators: it yields an `AgentToolEvent` the moment each
tool call finishes (so the caller can emit the SSE `tool` event live,
SPEC.md §5 — not batched at the end) and finally yields one `AgentDone`
carrying the `AgentResult`. Callers should iterate to exhaustion and take
the last event's `.result`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.agent.prompts import AGENT_SYSTEM_PROMPT, BUDGET_EXHAUSTED_NOTE, build_agent_seed_message
from app.agent.tools import TOOL_SPECS, ToolContext, execute_tool
from app.models import RetrievedChunk
from app.providers.llm import LLMProvider, LLMUsage

# Rough chars/4 approximation, consistent with the rest of the codebase
# (index/store.py's chunks.token_count, retrieval/expand.py's token budget)
# — good enough for a budget cutoff, not an exact tokenizer count.
_CHARS_PER_TOKEN = 4
# One runaway tool result (e.g. a huge grep match set) shouldn't alone blow
# the whole context budget — cap what any single tool_result contributes.
_MAX_TOOL_RESULT_CHARS = 4000


@dataclass
class ToolCallRecord:
    name: str
    input: dict
    result_summary: str


@dataclass
class AgentResult:
    text: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(input_tokens=0, output_tokens=0))
    stopped_reason: str = "answered"  # "answered" | "budget_exhausted"


@dataclass
class AgentToolEvent:
    record: ToolCallRecord


@dataclass
class AgentDone:
    result: AgentResult


AgentEvent = AgentToolEvent | AgentDone


def run_agent(
    llm_provider: LLMProvider,
    tool_ctx: ToolContext,
    question: str,
    seed_chunks: list[RetrievedChunk],
    *,
    max_iterations: int,
    max_context_tokens: int,
    max_wall_seconds: int,
) -> Iterator[AgentEvent]:
    start = time.monotonic()
    seed_message = build_agent_seed_message(question, seed_chunks)
    messages: list[dict] = [{"role": "user", "content": seed_message}]
    tool_call_records: list[ToolCallRecord] = []
    total_input_tokens = 0
    total_output_tokens = 0
    context_chars = len(AGENT_SYSTEM_PROMPT) + len(seed_message)

    for iteration in range(max(1, max_iterations)):
        budget_exhausted = (
            iteration == max_iterations - 1
            or context_chars >= max_context_tokens * _CHARS_PER_TOKEN
            or (time.monotonic() - start) >= max_wall_seconds
        )
        if budget_exhausted:
            messages.append({"role": "user", "content": BUDGET_EXHAUSTED_NOTE})
            context_chars += len(BUDGET_EXHAUSTED_NOTE)

        resp = llm_provider.complete_with_tools(
            system=AGENT_SYSTEM_PROMPT, messages=messages, tools=TOOL_SPECS, max_tokens=1500
        )
        if resp.usage:
            total_input_tokens += resp.usage.input_tokens
            total_output_tokens += resp.usage.output_tokens

        if not resp.tool_calls or budget_exhausted:
            yield AgentDone(
                AgentResult(
                    text=resp.text,
                    tool_calls=tool_call_records,
                    usage=LLMUsage(input_tokens=total_input_tokens, output_tokens=total_output_tokens),
                    stopped_reason="budget_exhausted" if budget_exhausted else "answered",
                )
            )
            return

        # Anthropic's multi-turn tool-use shape: the assistant's tool_use
        # blocks, then a user turn carrying the matching tool_result blocks
        # (see providers/llm.py's module docstring for why `messages` is
        # Anthropic-shaped here, unlike complete()/stream()'s plain strings).
        assistant_content: list[dict] = []
        if resp.text:
            assistant_content.append({"type": "text", "text": resp.text})
        assistant_content += [
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input} for tc in resp.tool_calls
        ]
        messages.append({"role": "assistant", "content": assistant_content})

        tool_result_blocks: list[dict] = []
        for tc in resp.tool_calls:
            result, summary = execute_tool(tc.name, tool_ctx, tc.input)
            record = ToolCallRecord(name=tc.name, input=tc.input, result_summary=summary)
            tool_call_records.append(record)
            yield AgentToolEvent(record)

            result_text = json.dumps(result, default=str)[:_MAX_TOOL_RESULT_CHARS]
            context_chars += len(result_text)
            tool_result_blocks.append({"type": "tool_result", "tool_use_id": tc.id, "content": result_text})
        messages.append({"role": "user", "content": tool_result_blocks})

    # Unreachable in practice — the loop always yields an AgentDone via the
    # `budget_exhausted` branch on its final allowed iteration — but keeps
    # this a well-formed generator (always yields at least one AgentDone)
    # rather than relying on that invariant silently.
    yield AgentDone(
        AgentResult(
            text="",
            tool_calls=tool_call_records,
            usage=LLMUsage(input_tokens=total_input_tokens, output_tokens=total_output_tokens),
            stopped_reason="budget_exhausted",
        )
    )
