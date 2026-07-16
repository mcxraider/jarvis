"""Isolation tests for dependencies injected into a shared graph node."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.graph import StateGraph

from agents.agent_api.app.graph.nodes.confirm import create_confirm_node
from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState


class _RecordingTracer:
    def __init__(self) -> None:
        self.events = []
        self.progress_events = []

    def event(self, stage, message, **fields):
        self.events.append((stage, message, fields))

    def progress(self, payload):
        self.progress_events.append(payload)


class _Dispatcher:
    def __init__(self, name: str) -> None:
        self.name = name
        self.registry = {}
        self.tool_node_builds = 0

    def build_langchain_tools(self):
        self.tool_node_builds += 1
        return []


def _config(deps: RunDeps) -> dict:
    return {"configurable": {"deps": deps}}


def _tool_state() -> dict:
    return {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "probe", "arguments": "{}"},
                    }
                ],
            }
        ],
        "tool_results": [],
        "thread_id": "thread-1",
        "turn_count": 1,
    }


def test_deps_from_config_accepts_only_the_run_container() -> None:
    deps = RunDeps(tool_selector=object(), run_control=object())

    assert deps_from_config(_config(deps)) is deps
    assert deps_from_config(None) is None
    assert deps_from_config({}) is None
    assert deps_from_config({"configurable": {"deps": SimpleNamespace()}}) is None


def test_configured_tracer_does_not_replace_direct_call_fallback() -> None:
    captured_tracer = _RecordingTracer()
    configured_tracer = _RecordingTracer()
    node = create_confirm_node(captured_tracer)
    state = {"held_calls": None, "messages": []}

    asyncio.run(node(state, _config(RunDeps(tracer=configured_tracer))))
    asyncio.run(node(state))

    assert [event[0] for event in configured_tracer.events] == ["graph.confirm"]
    assert [event[0] for event in captured_tracer.events] == ["graph.confirm"]


def test_compiled_node_receives_isolated_runnable_config_dependencies() -> None:
    captured_tracer = _RecordingTracer()
    configured_tracers = {name: _RecordingTracer() for name in ("alpha", "beta")}
    workflow = StateGraph(JarvisState)
    workflow.add_node("confirm", create_confirm_node(captured_tracer))
    workflow.set_entry_point("confirm")
    workflow.set_finish_point("confirm")
    app = workflow.compile()
    state = {"held_calls": None, "messages": []}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            name: pool.submit(
                asyncio.run,
                app.ainvoke(state, _config(RunDeps(tracer=tracer))),
            )
            for name, tracer in configured_tracers.items()
        }
        results = {name: future.result(timeout=5) for name, future in futures.items()}

    assert all(result["next"] == "end" for result in results.values())
    assert captured_tracer.events == []
    assert all(
        [event[0] for event in tracer.events] == ["graph.confirm"]
        for tracer in configured_tracers.values()
    )


def test_shared_tools_node_isolates_concurrent_run_dispatchers_and_tracers() -> None:
    captured_dispatcher = _Dispatcher("captured")
    captured_tracer = _RecordingTracer()
    partial_config_tracer = _RecordingTracer()
    node = create_tools_node(captured_dispatcher, captured_tracer)

    dispatchers = {name: _Dispatcher(name) for name in ("alpha", "beta")}
    tracers = {name: _RecordingTracer() for name in dispatchers}
    controls = {name: object() for name in dispatchers}
    selectors = {name: object() for name in dispatchers}
    deps = {
        name: RunDeps(
            dispatcher=dispatcher,
            tracer=tracers[name],
            tool_selector=selectors[name],
            run_control=controls[name],
        )
        for name, dispatcher in dispatchers.items()
    }

    async def execute(_calls, dispatcher):
        if dispatcher.name in dispatchers:
            entered.add(dispatcher.name)
            if entered == set(dispatchers):
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5)
        return [
            {
                "tool_call_id": "call-1",
                "tool_name": "probe",
                "success": True,
                "content": {"dispatcher": dispatcher.name},
                "error": None,
            }
        ]

    entered = set()
    both_entered = asyncio.Event()

    async def run_calls():
        results = dict(
            zip(
                dispatchers,
                await asyncio.gather(
                    *(
                        node(_tool_state(), _config(deps[name]))
                        for name in dispatchers
                    )
                ),
            )
        )
        second_alpha = await node(_tool_state(), _config(deps["alpha"]))
        direct = await node(_tool_state())
        partial_config = await node(
            _tool_state(),
            _config(RunDeps(tracer=partial_config_tracer)),
        )
        return results, second_alpha, direct, partial_config

    with patch(
        "agents.agent_api.app.graph.nodes.tools.async_execute_tool_calls",
        side_effect=execute,
    ):
        results, second_alpha, direct, partial_config = asyncio.run(run_calls())

    assert results["alpha"]["tool_results"][0]["content"] == {"dispatcher": "alpha"}
    assert results["beta"]["tool_results"][0]["content"] == {"dispatcher": "beta"}
    assert second_alpha["tool_results"][0]["content"] == {"dispatcher": "alpha"}
    assert direct["tool_results"][0]["content"] == {"dispatcher": "captured"}
    assert partial_config["tool_results"][0]["content"] == {"dispatcher": "captured"}

    assert dispatchers["alpha"].tool_node_builds == 0
    assert dispatchers["beta"].tool_node_builds == 0
    assert captured_dispatcher.tool_node_builds == 0
    assert tracers["alpha"].events and tracers["beta"].events
    assert captured_tracer.events
    assert partial_config_tracer.events
    assert deps["alpha"].tool_selector is selectors["alpha"]
    assert deps["beta"].run_control is controls["beta"]
