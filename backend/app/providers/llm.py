"""LLMProvider protocol + adapters (SPEC.md §7.2).

`stream(...)` yields `str` text deltas and, as its final item, one `LLMUsage`
— avoiding shared mutable state on the provider instance (which would race
under concurrent requests) while still surfacing token counts for
`query_log` once the stream is exhausted.

`complete_with_tools`'s *response* shape (Phase 3, `ToolUseResponse`/
`ToolCall`) is fully vendor-neutral, same spirit as `LLMResponse` — agent/
loop.py never sees an Anthropic SDK type. Its *request* shape (`messages`)
is the one place this module doesn't fully abstract the vendor away:
`complete`/`stream` use plain `{"role": ..., "content": <str>}` dicts, but a
multi-turn tool-use conversation needs structured content (an assistant
turn's `tool_use` blocks, a user turn's matching `tool_result` blocks) —
Anthropic's native block shape, passed straight through by
`AnthropicLLMProvider` and accepted as-is by `FakeLLMProvider` (which only
ever reads the parts it needs, never an SDK type). Building a second,
fully vendor-neutral message-content abstraction just for tool
conversations was judged not worth it for a single-provider (Anthropic)
project — see DECISIONS.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from app.config import Settings


@dataclass
class LLMUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class LLMResponse:
    text: str
    usage: LLMUsage


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolUseResponse:
    text: str  # any plain text the model produced alongside/before its tool calls
    tool_calls: list[ToolCall] = field(default_factory=list)  # [] means "final answer, no more tools"
    stop_reason: str | None = None
    usage: LLMUsage | None = None


class LLMProvider(Protocol):
    model: str

    def complete(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> LLMResponse: ...

    def stream(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> Iterator[str | LLMUsage]: ...

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> ToolUseResponse: ...


class FakeLLMProvider:
    """Deterministic, zero-network provider for tests. By default,
    synthesizes an answer that cites the first `[path:start-end]`-shaped
    reference found in the prompt, so tests can assert the citation
    plumbing works end-to-end without a real model call. Pass `responses`
    to script exact multi-turn behavior instead (e.g. testing citations.py's
    regenerate-once path: a first call returning a bad citation, a second
    returning a good one) — one list entry popped per `complete`/`stream`
    call. Pass `tool_responses` to script `complete_with_tools` calls the
    same way — one `ToolUseResponse` popped per call (agent/loop.py tests).
    """

    model = "fake-llm"

    def __init__(
        self,
        responses: list[str] | None = None,
        tool_responses: list[ToolUseResponse] | None = None,
    ) -> None:
        self._responses = list(responses) if responses is not None else None
        self._tool_responses = list(tool_responses) if tool_responses is not None else None

    def complete(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> LLMResponse:
        text = self._next_answer(messages)
        return LLMResponse(text=text, usage=self._usage(system, messages, text))

    def stream(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> Iterator[str | LLMUsage]:
        text = self._next_answer(messages)
        for word in text.split(" "):
            yield word + " "
        yield self._usage(system, messages, text)

    def _next_answer(self, messages: list[dict[str, str]]) -> str:
        if self._responses is not None:
            if not self._responses:
                raise AssertionError("FakeLLMProvider ran out of scripted responses")
            return self._responses.pop(0)
        return self._answer(messages)

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> ToolUseResponse:
        if self._tool_responses is None:
            raise AssertionError(
                "FakeLLMProvider.complete_with_tools requires tool_responses= to be scripted — "
                "there's no sensible default the way complete()/stream() have one, since a caller "
                "needs to control exactly when the fake agent stops calling tools."
            )
        if not self._tool_responses:
            raise AssertionError("FakeLLMProvider ran out of scripted tool_responses")
        return self._tool_responses.pop(0)

    @staticmethod
    def _usage(system: str, messages: list[dict[str, str]], text: str) -> LLMUsage:
        input_tokens = len(system.split()) + sum(len(m.get("content", "").split()) for m in messages)
        return LLMUsage(input_tokens=input_tokens, output_tokens=len(text.split()))

    @staticmethod
    def _answer(messages: list[dict[str, str]]) -> str:
        blob = "\n".join(m.get("content", "") for m in messages)
        match = re.search(r"\[[\w./-]+:\d+-\d+\]", blob)
        cite = match.group(0) if match else "[unknown:0-0]"
        return f"This is a test answer citing {cite}."


class AnthropicLLMProvider:
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # lazy import: optional dependency

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def complete(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> LLMResponse:
        resp = self._client.messages.create(
            # cast: our protocol deliberately uses a generic dict shape, not
            # the SDK's exact MessageParam TypedDicts, to keep business logic
            # SDK-agnostic (SPEC.md §7.2).
            model=self.model, system=system, messages=cast(Any, messages), max_tokens=max_tokens
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens
            ),
        )

    def stream(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> Iterator[str | LLMUsage]:
        with self._client.messages.stream(
            model=self.model, system=system, messages=cast(Any, messages), max_tokens=max_tokens
        ) as stream:
            yield from stream.text_stream
            final = stream.get_final_message()
            yield LLMUsage(
                input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens
            )

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> ToolUseResponse:
        resp = self._client.messages.create(
            model=self.model,
            system=system,
            messages=cast(Any, messages),
            tools=cast(Any, tools),
            max_tokens=max_tokens,
        )
        text = "".join(block.text for block in resp.content if block.type == "text")
        tool_calls = [
            ToolCall(id=block.id, name=block.name, input=block.input)
            for block in resp.content
            if block.type == "tool_use"
        ]
        return ToolUseResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
            usage=LLMUsage(
                input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens
            ),
        )


def get_llm_provider(settings: Settings) -> LLMProvider:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the synthesis LLM")
    return AnthropicLLMProvider(settings.anthropic_api_key, settings.synthesis_model)


def get_summarization_llm_provider(settings: Settings) -> LLMProvider:
    """Same adapter, cheap model (`settings.summarization_model`) — used for
    high-volume, low-stakes calls like query routing (retrieval/router.py)
    and (Phase 3) repo/file summarization, where the full synthesis model
    would be needlessly expensive."""
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the summarization LLM")
    return AnthropicLLMProvider(settings.anthropic_api_key, settings.summarization_model)
