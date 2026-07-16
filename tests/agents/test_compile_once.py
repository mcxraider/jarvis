"""Compiled-graph reuse and request-dependency isolation tests."""

import asyncio
import threading
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agents.agent_api.app.graph import builder
from agents.agent_api.app.graph.nodes.orchestrator import UsageSummary
from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tracing import NULL_TRACE
from agents.agent_api.app.user_context.identity import telegram_identity
from agents.agent_api.app.user_context.runtime import ResolvedRuntimeContext
from langgraph.types import Command
from tests.agents.runtime_helpers import make_snapshot


class _FakeAgent:
    def __init__(self, content: str) -> None:
        self._content = content
        self.usage = UsageSummary()

    def create_message(self, messages, tools, **kwargs):
        self.usage = UsageSummary(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        return {"role": "assistant", "content": self._content}


_TODOIST_METHODS = (
    "add_todoist_task",
    "get_todoist_task",
    "get_tasks",
    "get_tasks_by_filter",
    "update_todoist_task",
    "complete_task",
    "uncomplete_task",
    "delete_todoist_task",
    "get_completed_todoist_tasks_by_completion_date",
    "get_comments",
    "add_comment",
    "get_labels",
    "get_projects",
    "create_project",
)


class _FakeTodoist:
    def __getattr__(self, name):
        if name in _TODOIST_METHODS:
            return lambda arguments: {}
        raise AttributeError(name)


class _RecordingTracer:
    def __init__(self) -> None:
        self.payloads: list[tuple[str, Any]] = []

    def section(self, *args, **kwargs) -> None:
        pass

    def event(self, *args, **kwargs) -> None:
        pass

    def progress(self, payload) -> None:
        pass

    def payload(self, stage, label, value, **kwargs) -> None:
        self.payloads.append((label, value))


def setup_function() -> None:
    builder.reset_compiled_graphs()


def teardown_function() -> None:
    builder.reset_compiled_graphs()


def test_multiple_runs_compile_once_for_the_same_checkpointer() -> None:
    real_build_graph = builder.build_graph
    compile_count = 0

    def counting_build_graph(*args, **kwargs):
        nonlocal compile_count
        compile_count += 1
        return real_build_graph(*args, **kwargs)

    with patch.object(builder, "build_graph", side_effect=counting_build_graph):
        for index in range(3):
            result = builder.run_jarvis(
                user_prompt=f"prompt-{index}",
                agent_client=_FakeAgent(f"answer-{index}"),
                todoist_client=_FakeTodoist(),
                tracer=NULL_TRACE,
            )
            assert result["final_response"] == f"answer-{index}"

    assert compile_count == 1


def test_async_runner_executes_inside_the_callers_event_loop() -> None:
    async def scenario() -> None:
        result = await builder.run_jarvis_async(
            user_prompt="async prompt",
            agent_client=_FakeAgent("async answer"),
            todoist_client=_FakeTodoist(),
            tracer=NULL_TRACE,
            checkpointer=builder.DEFAULT_CHECKPOINTER,
        )
        assert result["final_response"] == "async answer"

    asyncio.run(scenario())


def test_sync_adapter_rejects_calls_from_an_active_event_loop() -> None:
    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="await run_jarvis_async"):
            builder.run_jarvis(
                user_prompt="wrong surface",
                agent_client=_FakeAgent("unused"),
                todoist_client=_FakeTodoist(),
                tracer=NULL_TRACE,
            )

    asyncio.run(scenario())


def test_sync_adapter_reuses_one_loop_across_sequential_calls() -> None:
    class _AsyncAgent:
        def __init__(self) -> None:
            self.loops: list[asyncio.AbstractEventLoop] = []
            self.usage = UsageSummary()

        async def async_create_message(self, _messages, _tools, **_kwargs):
            self.loops.append(asyncio.get_running_loop())
            return {"role": "assistant", "content": "done"}

        def create_message(self, *_args, **_kwargs):
            raise AssertionError("sync client path used")

    agent = _AsyncAgent()
    closed_on: list[asyncio.AbstractEventLoop] = []

    async def close_resource() -> None:
        closed_on.append(asyncio.get_running_loop())

    with patch(
        "agents.agent_api.app.graph.nodes.orchestrator.close_shared_async_agent_client",
        new=close_resource,
    ), patch(
        "agents.agent_api.app.router.client.close_shared_async_router_openai_client",
        new=close_resource,
    ), patch(
        "agents.agent_api.app.graph.nodes.summarize.close_shared_async_summarizer_client",
        new=close_resource,
    ), patch(
        "agents.agent_api.app.tools.todoist.client.close_todoist_async_http_client",
        new=close_resource,
    ):
        try:
            for _ in range(2):
                result = builder.run_jarvis(
                    user_prompt="sequential",
                    agent_client=agent,
                    todoist_client=_FakeTodoist(),
                    tracer=NULL_TRACE,
                )
                assert result["final_response"] == "done"

            first_loop = agent.loops[0]
            builder.shutdown_sync_runner()
            builder.shutdown_sync_runner()
            assert builder._SYNC_RUNNER is None
            assert closed_on == [first_loop] * 4

            result = builder.run_jarvis(
                user_prompt="new runner",
                agent_client=agent,
                todoist_client=_FakeTodoist(),
                tracer=NULL_TRACE,
            )
            assert result["final_response"] == "done"
            assert agent.loops[-1] is not first_loop
        finally:
            builder.shutdown_sync_runner()

    assert len(agent.loops) == 3
    assert agent.loops[0] is agent.loops[1]


def test_async_runner_persists_fresh_context_before_graph_entry() -> None:
    events: list[str] = []
    context = ResolvedRuntimeContext(snapshot=make_snapshot(), credentials={})

    class _Graph:
        async def ainvoke(self, state, _config):
            assert events == ["stored"]
            assert not isinstance(state, Command)
            events.append("invoked")
            return {
                **state,
                "final_response": "done",
                "interrupted": False,
                "error": "",
            }

    async def store(*_args) -> None:
        events.append("stored")

    configured = replace(builder.settings, postgres_dsn="postgresql://test")
    with patch.object(builder, "settings", configured), patch.object(
        builder,
        "resolve_runtime_context_async",
        new=AsyncMock(return_value=context),
    ), patch.object(
        builder,
        "store_thread_context_async",
        new=AsyncMock(side_effect=store),
    ) as persist, patch.object(
        builder,
        "build_runtime_registry",
        return_value=(ToolRegistry(), [], {}),
    ), patch.object(
        builder,
        "get_or_compile_graph",
        return_value=_Graph(),
    ), patch.object(builder, "_register_thread"), patch.object(builder, "_log_usage"):
        result = asyncio.run(
            builder.run_jarvis_async(
                user_prompt="fresh",
                identity=telegram_identity(123),
                tracer=NULL_TRACE,
                checkpointer=builder.DEFAULT_CHECKPOINTER,
            )
        )

    assert result["final_response"] == "done"
    assert events == ["stored", "invoked"]
    persist.assert_awaited_once()


def test_async_runner_resumes_with_command_and_same_thread() -> None:
    context = ResolvedRuntimeContext(snapshot=make_snapshot(), credentials={})
    received: list[tuple[Any, dict]] = []

    class _Graph:
        async def ainvoke(self, state, config):
            received.append((state, config))
            return {
                "thread_id": "thread-resume",
                "messages": [],
                "tool_results": [],
                "final_response": "resumed",
                "interrupted": False,
                "error": "",
            }

    configured = replace(builder.settings, postgres_dsn="postgresql://test")
    with patch.object(builder, "settings", configured), patch.object(
        builder,
        "load_thread_runtime_context_async",
        new=AsyncMock(return_value=context),
    ) as load, patch.object(
        builder,
        "store_thread_context_async",
        new=AsyncMock(),
    ) as persist, patch.object(
        builder,
        "build_runtime_registry",
        return_value=(ToolRegistry(), [], {}),
    ), patch.object(
        builder,
        "get_or_compile_graph",
        return_value=_Graph(),
    ), patch.object(builder, "_register_thread"), patch.object(builder, "_log_usage"):
        result = asyncio.run(
            builder.run_jarvis_async(
                user_prompt="approve",
                thread_id="thread-resume",
                clarification_reply="approve",
                identity=telegram_identity(123),
                tracer=NULL_TRACE,
                checkpointer=builder.DEFAULT_CHECKPOINTER,
            )
        )

    command, config = received[0]
    assert isinstance(command, Command)
    assert command.resume == "approve"
    assert config["configurable"]["thread_id"] == "thread-resume"
    assert result["final_response"] == "resumed"
    load.assert_awaited_once()
    persist.assert_not_awaited()


def test_injected_agent_legacy_usage_is_preserved() -> None:
    agent = _FakeAgent("answer")

    with patch.object(builder, "_log_usage") as log_usage:
        builder.run_jarvis(
            user_prompt="prompt",
            agent_client=agent,
            todoist_client=_FakeTodoist(),
            tracer=NULL_TRACE,
        )

    usage = log_usage.call_args.args[2]
    assert usage.as_dict() == {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_shared_graph_isolates_concurrent_run_dependencies() -> None:
    builder.get_or_compile_graph()
    barrier = threading.Barrier(2)
    tracers = {"A": _RecordingTracer(), "B": _RecordingTracer()}
    results: dict[str, Any] = {}
    failures: list[BaseException] = []

    def run(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = builder.run_jarvis(
                user_prompt=f"prompt-{name}",
                agent_client=_FakeAgent(f"answer-{name}"),
                todoist_client=_FakeTodoist(),
                tracer=tracers[name],
            )
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert results["A"]["final_response"] == "answer-A"
    assert results["B"]["final_response"] == "answer-B"
    assert [value for label, value in tracers["A"].payloads if label == "user_prompt"] == [
        "prompt-A"
    ]
    assert [value for label, value in tracers["B"].payloads if label == "user_prompt"] == [
        "prompt-B"
    ]
