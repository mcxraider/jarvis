"""Compiled-graph reuse and request-dependency isolation tests."""

import threading
from typing import Any
from unittest.mock import patch

from agents.agent_api.app.graph import builder
from agents.agent_api.app.graph.nodes.orchestrator import UsageSummary
from agents.agent_api.app.tracing import NULL_TRACE


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
