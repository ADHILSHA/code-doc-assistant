"""Agent loop (SPEC.md §6 Phase 3 task 3): budgets, tool dispatch, and live
per-tool-call events, against the real indexed mini_repo fixture using
FakeLLMProvider's scripted `tool_responses`.

`run_agent` is a generator (see agent/loop.py's module docstring for why);
`_run(...)` here just drains it and returns `(tool_events, final_result)`
so individual tests don't repeat that boilerplate.
"""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.agent.loop import AgentDone, AgentResult, AgentToolEvent, ToolCallRecord, run_agent
from app.agent.tools import ToolContext
from app.db import get_registry_connection, get_repo_connection
from app.models import RetrievedChunk
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider, LLMUsage, ToolCall, ToolUseResponse

from .conftest import MINI_REPO, make_settings


def _tool_context(tmp_path: Path) -> ToolContext:
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    registry_conn = get_registry_connection(settings)
    local_path = registry_conn.execute(
        "SELECT local_path FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["local_path"]
    return ToolContext(
        conn=conn, repo_root=Path(local_path), embedding_provider=FakeEmbeddingProvider(), settings=settings
    )


def _run(*args, **kwargs) -> tuple[list[ToolCallRecord], AgentResult]:
    tool_events: list[ToolCallRecord] = []
    result: AgentResult | None = None
    for event in run_agent(*args, **kwargs):
        if isinstance(event, AgentToolEvent):
            tool_events.append(event.record)
        elif isinstance(event, AgentDone):
            result = event.result
    assert result is not None, "run_agent must always yield exactly one AgentDone"
    return tool_events, result


def test_agent_stops_immediately_when_first_response_has_no_tool_calls(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    fake = FakeLLMProvider(
        tool_responses=[ToolUseResponse(text="Direct answer, no tools needed.", tool_calls=[])]
    )
    tool_events, result = _run(
        fake, ctx, "What does this repo do?", [],
        max_iterations=15, max_context_tokens=60_000, max_wall_seconds=40,
    )
    assert result.text == "Direct answer, no tools needed."
    assert result.tool_calls == []
    assert tool_events == []
    assert result.stopped_reason == "answered"


def test_agent_executes_a_tool_call_and_uses_its_result(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    fake = FakeLLMProvider(
        tool_responses=[
            ToolUseResponse(
                text="Checking the definition.",
                tool_calls=[ToolCall(id="t1", name="get_definition", input={"symbol": "UserService"})],
                stop_reason="tool_use",
                usage=LLMUsage(10, 5),
            ),
            ToolUseResponse(
                text="`UserService` is defined in [src/users/service.py:21-44].",
                tool_calls=[],
                stop_reason="end_turn",
                usage=LLMUsage(20, 15),
            ),
        ]
    )
    tool_events, result = _run(
        fake, ctx, "Where is UserService defined?", [],
        max_iterations=15, max_context_tokens=60_000, max_wall_seconds=40,
    )
    assert "src/users/service.py:21-44" in result.text
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_definition"
    assert "1 definition" in result.tool_calls[0].result_summary
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 20
    assert result.stopped_reason == "answered"
    # the live tool_call event stream matches the final accumulated list
    assert tool_events == result.tool_calls


def test_tool_events_are_yielded_live_before_the_final_done_event(tmp_path: Path):
    """Order matters: the caller (generation/answer.py) emits an SSE `tool`
    event per AgentToolEvent as it arrives, so AgentDone must always be
    last."""
    ctx = _tool_context(tmp_path)
    fake = FakeLLMProvider(
        tool_responses=[
            ToolUseResponse(
                text="", tool_calls=[ToolCall(id="t1", name="get_dependencies", input={})], stop_reason="tool_use"
            ),
            ToolUseResponse(text="done", tool_calls=[]),
        ]
    )
    events = list(run_agent(fake, ctx, "q", [], max_iterations=15, max_context_tokens=60_000, max_wall_seconds=40))
    assert isinstance(events[-1], AgentDone)
    assert all(isinstance(e, AgentToolEvent) for e in events[:-1])


def test_agent_forces_a_final_answer_when_iteration_budget_is_exhausted(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    keeps_wanting_tools = ToolUseResponse(
        text="",
        tool_calls=[ToolCall(id="tX", name="get_dependencies", input={})],
        stop_reason="tool_use",
        usage=LLMUsage(5, 5),
    )
    final_answer_anyway = ToolUseResponse(
        text="Final answer despite wanting more tools.", tool_calls=[], usage=LLMUsage(5, 5)
    )
    fake = FakeLLMProvider(tool_responses=[keeps_wanting_tools, final_answer_anyway])

    _tool_events, result = _run(
        fake, ctx, "trace something", [],
        max_iterations=2, max_context_tokens=60_000, max_wall_seconds=40,
    )
    assert result.text == "Final answer despite wanting more tools."
    assert result.stopped_reason == "budget_exhausted"
    assert len(result.tool_calls) == 1  # only the first iteration's tool call ran


def test_agent_respects_wall_clock_budget(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    fake = FakeLLMProvider(tool_responses=[ToolUseResponse(text="answered under time pressure", tool_calls=[])])
    _tool_events, result = _run(
        fake, ctx, "q", [], max_iterations=15, max_context_tokens=60_000, max_wall_seconds=0,
    )
    assert result.stopped_reason == "budget_exhausted"
    assert result.text == "answered under time pressure"


def test_agent_respects_context_token_budget(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    fake = FakeLLMProvider(tool_responses=[ToolUseResponse(text="answered under a tiny token budget", tool_calls=[])])
    _tool_events, result = _run(
        fake, ctx, "q" * 500, [], max_iterations=15, max_context_tokens=1, max_wall_seconds=40,
    )
    assert result.stopped_reason == "budget_exhausted"


def test_agent_seeds_the_first_message_with_hybrid_results(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    seed = [
        RetrievedChunk(
            chunk_id=1, file_path="src/auth/auth.py", start_line=17, end_line=21,
            content="def hash_password(pw): ...",
        )
    ]
    captured_messages: list = []

    class _CapturingFake(FakeLLMProvider):
        def complete_with_tools(self, *, system, messages, tools, max_tokens=1024):
            captured_messages.extend(messages)
            return super().complete_with_tools(system=system, messages=messages, tools=tools, max_tokens=max_tokens)

    fake = _CapturingFake(tool_responses=[ToolUseResponse(text="ok", tool_calls=[])])
    _run(fake, ctx, "How does hashing work?", seed, max_iterations=15, max_context_tokens=60_000, max_wall_seconds=40)

    assert len(captured_messages) == 1
    assert "src/auth/auth.py:17-21" in captured_messages[0]["content"]
    assert "def hash_password" in captured_messages[0]["content"]


def test_agent_handles_multiple_tool_calls_in_one_turn(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    fake = FakeLLMProvider(
        tool_responses=[
            ToolUseResponse(
                text="",
                tool_calls=[
                    ToolCall(id="a", name="get_dependencies", input={}),
                    ToolCall(id="b", name="list_endpoints", input={}),
                ],
                stop_reason="tool_use",
            ),
            ToolUseResponse(text="done", tool_calls=[]),
        ]
    )
    _tool_events, result = _run(fake, ctx, "q", [], max_iterations=15, max_context_tokens=60_000, max_wall_seconds=40)
    assert {tc.name for tc in result.tool_calls} == {"get_dependencies", "list_endpoints"}
