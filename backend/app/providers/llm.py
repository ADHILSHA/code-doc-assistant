"""LLMProvider protocol + adapters (SPEC.md §7.2).

`stream(...)` yields `str` text deltas and, as its final item, one `LLMUsage`
— avoiding shared mutable state on the provider instance (which would race
under concurrent requests) while still surfacing token counts for
`query_log` once the stream is exhausted.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
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


class LLMProvider(Protocol):
    model: str

    def complete(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> LLMResponse: ...

    def stream(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> Iterator[str | LLMUsage]: ...

    # Exact tool-schema shape is finalized in Phase 3 when agent/tools.py
    # defines the tool specs; declared now so the provider shape matches
    # SPEC.md §7.2 and adapters don't need reshaping later.
    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> Any: ...


class FakeLLMProvider:
    """Deterministic, zero-network provider for tests. Synthesizes an answer
    that cites the first `[path:start-end]`-shaped reference found in the
    prompt, so tests can assert the citation plumbing works end-to-end
    without a real model call.
    """

    model = "fake-llm"

    def complete(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> LLMResponse:
        text = self._answer(messages)
        return LLMResponse(text=text, usage=self._usage(system, messages, text))

    def stream(
        self, *, system: str, messages: list[dict[str, str]], max_tokens: int = 1024
    ) -> Iterator[str | LLMUsage]:
        text = self._answer(messages)
        for word in text.split(" "):
            yield word + " "
        yield self._usage(system, messages, text)

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> Any:
        raise NotImplementedError(
            "FakeLLMProvider.complete_with_tools lands in Phase 3 with the agent tool schema"
        )

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
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
    ) -> Any:
        return self._client.messages.create(
            model=self.model,
            system=system,
            messages=cast(Any, messages),
            tools=cast(Any, tools),
            max_tokens=max_tokens,
        )


def get_llm_provider(settings: Settings) -> LLMProvider:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the synthesis LLM")
    return AnthropicLLMProvider(settings.anthropic_api_key, settings.synthesis_model)
