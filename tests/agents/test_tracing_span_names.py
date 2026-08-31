"""Tests for LangSmith trace span naming conventions (Task 1).

Two concerns:
(a) name_current_run — renames the active run, safe no-op when tracing is off.
(b) Graph compile smoke check — node keys and edge spans are correctly named.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agents.agent_api.app.tracing as tracing_module
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter, name_current_run
from agents.agent_api.app.checkpointing import InMemorySaver
from agents.agent_api.app.graph.builder import create_jarvis_graph
from langsmith import traceable, tracing_context


# ---------------------------------------------------------------------------
# (a) name_current_run
# ---------------------------------------------------------------------------


def test_name_current_run_renames_run_tree() -> None:
    """When get_current_run_tree returns a run, its name is updated."""
    fake_run = SimpleNamespace(name="old")
    with patch.object(tracing_module, "get_current_run_tree", return_value=fake_run):
        name_current_run("tool.todoist_get_tasks")
    assert fake_run.name == "tool.todoist_get_tasks"


def test_name_current_run_noop_when_no_run_tree() -> None:
    """When tracing is disabled (get_current_run_tree returns None), no exception."""
    with patch.object(tracing_module, "get_current_run_tree", return_value=None):
        name_current_run("tool.todoist_get_tasks")  # must not raise


def test_name_current_run_swallows_exception() -> None:
    """Exceptions from get_current_run_tree are swallowed."""
    with patch.object(tracing_module, "get_current_run_tree", side_effect=RuntimeError("oops")):
        name_current_run("tool.todoist_get_tasks")  # must not raise


# ---------------------------------------------------------------------------
# (b) Graph compile smoke check
# ---------------------------------------------------------------------------


def test_graph_compiles_with_prefixed_node_keys() -> None:
    """Graph compiles and conditional-edge spans carry the new prefixed names."""
    checkpointer = InMemorySaver()
    app = create_jarvis_graph(checkpointer=checkpointer)

    # The branch channel names are "branch:to:<node>" — check two representative ones.
    assert "branch:to:graph.orchestrator" in app.channels, (
        "Expected branch:to:graph.orchestrator channel; node may still be named 'agent'"
    )
    assert "branch:to:graph.clarify" in app.channels, (
        "Expected branch:to:graph.clarify channel; node may still be named 'hitl'"
    )

    # Conditional-edge routing functions should be renamed to "<node>.route".
    # LangGraph stores branch specs under the node name in the compiled graph's
    # builder; the easiest observable surface is the branch channel names themselves
    # (already checked above). Additionally verify via the compiled graph's node list.
    node_names = set(app.nodes.keys())
    expected = {
        "graph.orchestrator",
        "graph.validate_entities",
        "graph.clarify",
        "graph.tools",
        "graph.summarize",
        "graph.prepare_confirm",
        "graph.confirm",
        "graph.executor",
        "graph.end",
    }
    assert expected.issubset(node_names), (
        f"Missing nodes: {expected - node_names}"
    )

    # END is a sentinel, not a node, so the terminal span only exists if every exit
    # routes through the real graph.end node.
    assert "branch:to:graph.end" in app.channels, (
        "Expected branch:to:graph.end channel; exits may still route straight to END"
    )
    for node, key in (("graph.orchestrator", "end"), ("graph.confirm", "decline"), ("graph.validate_entities", "end")):
        targets = app.builder.branches[node][f"{node}.route"].ends  # type: ignore[attr-defined]
        assert targets[key] == "graph.end", f"{node}[{key}] should route to graph.end, got {targets[key]}"

    # Verify edge router __name__ values via the graph's builder branches dict.
    # Each key in workflow._graph.branches[node_name] is the router __name__.
    branches = app.builder.branches  # type: ignore[attr-defined]
    router_names = {name for node_branches in branches.values() for name in node_branches}
    assert "graph.orchestrator.route" in router_names, (
        f"Expected graph.orchestrator.route in branch names, got: {router_names}"
    )
    assert "graph.validate_entities.route" in router_names
    assert "graph.tools.route" in router_names
    assert "graph.confirm.route" in router_names


# ---------------------------------------------------------------------------
# (c) Composition: @traceable + name_current_run → client receives renamed name
# ---------------------------------------------------------------------------


def test_traceable_name_current_run_composition() -> None:
    """@traceable(name="tool") + name_current_run("tool.X") → client.update_run receives name="tool.X".

    Guards the composition so that removing @traceable or moving name_current_run
    after an early return cannot silently break span naming while keeping the suite green.

    Uses a MagicMock client injected via tracing_context to avoid any network call.
    Synchronous: patch() is called inline when the traceable function exits.
    """
    fake_client = MagicMock()

    @traceable(name="tool", run_type="tool")
    def _fake_execute(tool_name: str) -> str:
        name_current_run(f"tool.{tool_name}")
        return "ok"

    with tracing_context(enabled=True, client=fake_client):
        _fake_execute("todoist_get_tasks")

    submitted_names = [
        call.kwargs["name"]
        for call in fake_client.update_run.call_args_list
        if "name" in call.kwargs
    ]
    assert "tool.todoist_get_tasks" in submitted_names, (
        f"Expected client.update_run called with name='tool.todoist_get_tasks', got: {submitted_names}"
    )


# ---------------------------------------------------------------------------
# (d) TracePrinter.event() → LangSmith span events
# ---------------------------------------------------------------------------


def _submitted_events(fake_client: MagicMock) -> list:
    """Flatten every events list submitted via client.update_run."""
    events = []
    for call in fake_client.update_run.call_args_list:
        for event in call.kwargs.get("events") or []:
            events.append(event)
    return events


def _run_traced(body) -> list:
    """Run `body` inside a live @traceable span, return the events reaching the wire."""
    fake_client = MagicMock()

    @traceable(name="span", run_type="chain")
    def _wrapped() -> str:
        body()
        return "ok"

    with tracing_context(enabled=True, client=fake_client):
        _wrapped()
    return _submitted_events(fake_client)


def test_event_adds_span_event_with_stage_message_and_fields() -> None:
    """event() under a live span reaches the wire carrying stage, message and fields."""
    tracer = TracePrinter(enabled=True)
    events = _run_traced(lambda: tracer.event("router.fast_path", "matched", domain="todoist"))

    matching = [e for e in events if e.get("name") == "router.fast_path"]
    assert matching, f"Expected a router.fast_path span event, got: {events}"
    assert matching[0]["message"] == "matched"
    assert matching[0]["domain"] == "todoist"
    assert "time" in matching[0]


def test_event_without_run_tree_does_not_raise() -> None:
    """Outside any span (NULL_TRACE path) event() is a silent no-op."""
    NULL_TRACE.event("router.fast_path", "matched", domain="todoist")  # must not raise


def test_event_keeps_scalars_truncates_strings_and_drops_none() -> None:
    """Ints/bools stay typed, long strings truncate to 180, None fields are dropped."""
    tracer = TracePrinter(enabled=True)
    long_text = "x" * 400
    events = _run_traced(
        lambda: tracer.event(
            "agent.response",
            "done",
            prompt_tokens=1234,
            cache_hit=True,
            ratio=0.5,
            long_field=long_text,
            missing=None,
        )
    )

    matching = [e for e in events if e.get("name") == "agent.response"]
    assert matching, f"Expected an agent.response span event, got: {events}"
    event = matching[0]
    assert event["prompt_tokens"] == 1234 and isinstance(event["prompt_tokens"], int)
    assert event["cache_hit"] is True
    assert event["ratio"] == 0.5
    assert len(event["long_field"]) == 180 and event["long_field"].endswith("...")
    assert "missing" not in event


def test_event_bridges_when_terminal_printing_disabled() -> None:
    """enabled=False gates terminal printing only — the span still gets the event."""
    tracer = TracePrinter(enabled=False)
    events = _run_traced(lambda: tracer.event("model_router.selected", "chose model"))

    assert any(e.get("name") == "model_router.selected" for e in events), (
        f"enabled=False must still bridge to LangSmith, got: {events}"
    )


def test_event_capped_at_max_span_events() -> None:
    """Past _MAX_SPAN_EVENTS further events are dropped rather than growing unbounded."""
    tracer = TracePrinter(enabled=False)
    cap = tracing_module._MAX_SPAN_EVENTS

    def _flood() -> None:
        for index in range(cap + 50):
            tracer.event("graph.executor", f"call {index}")

    events = _run_traced(_flood)
    executor_events = [e for e in events if e.get("name") == "graph.executor"]
    assert len(executor_events) == cap, (
        f"Expected exactly {cap} events after flooding, got {len(executor_events)}"
    )


def test_payload_is_not_bridged_to_span() -> None:
    """payload() is the raw-data channel and must stay terminal-only."""
    tracer = TracePrinter(enabled=True, show_payloads=True)
    events = _run_traced(lambda: tracer.payload("todoist.request", "body", {"secret": "x"}))

    assert not [e for e in events if e.get("name") == "todoist.request"], (
        f"payload() must not reach LangSmith, got: {events}"
    )


def test_event_inside_langgraph_node_lands_on_node_span() -> None:
    """An event emitted inside a compiled LangGraph node attaches to that node's span."""
    from typing import Any as _Any, Dict as _Dict

    from langgraph.graph import END, START, StateGraph

    tracer = TracePrinter(enabled=False)

    def _node(state: _Dict[str, _Any]) -> _Dict[str, _Any]:
        tracer.event("graph.validate", "verified ids", verified=3)
        return {"done": True}

    builder = StateGraph(dict)
    builder.add_node("graph.validate_entities", _node)
    builder.add_edge(START, "graph.validate_entities")
    builder.add_edge("graph.validate_entities", END)
    app = builder.compile()

    fake_client = MagicMock()
    with tracing_context(enabled=True, client=fake_client):
        app.invoke({})

    node_runs = [
        call
        for call in fake_client.update_run.call_args_list
        if call.kwargs.get("events")
        and any(e.get("name") == "graph.validate" for e in call.kwargs["events"])
    ]
    assert node_runs, (
        "Expected a graph.validate event on a submitted run; "
        f"calls: {[c.kwargs.get('name') for c in fake_client.update_run.call_args_list]}"
    )
    event = next(e for e in node_runs[0].kwargs["events"] if e.get("name") == "graph.validate")
    assert event["message"] == "verified ids"
    assert event["verified"] == 3


# ---------------------------------------------------------------------------
# (e) Root run + terminal node: what a completed run actually submits
# ---------------------------------------------------------------------------


def test_completed_run_submits_root_and_graph_end_spans() -> None:
    """An offline run of the real graph submits a jarvis.invoke root and a graph.end span.

    Two regressions this catches:
    - END is a sentinel with no span, so repointing any exit straight at it loses
      the terminal span.
    - Without the @traceable on run_jarvis_async there is no root run, and the
      runtime.* events emitted around ainvoke are silently dropped.
    """
    import asyncio

    from agents.agent_api.app.graph import builder as builder_module

    class OneShotAgentClient:
        def create_message(self, messages, tools, **kwargs):
            del messages, tools, kwargs
            return {"role": "assistant", "content": "Done."}

    fake_client = MagicMock()
    with tracing_context(enabled=True, client=fake_client):
        asyncio.run(
            builder_module.run_jarvis_async(
                user_prompt="say hi",
                agent_client=OneShotAgentClient(),
                tracer=TracePrinter(enabled=False),
                checkpointer=InMemorySaver(),
            )
        )

    submitted_names = {
        call.kwargs.get("name")
        for call in (*fake_client.create_run.call_args_list, *fake_client.update_run.call_args_list)
    }
    assert "jarvis.invoke" in submitted_names, (
        f"Expected a jarvis.invoke root run; got: {sorted(n for n in submitted_names if n)}"
    )
    assert "graph.end" in submitted_names, (
        "Run never reached the graph.end node; exits may route straight to END. "
        f"Got: {sorted(n for n in submitted_names if n)}"
    )
    assert any(e.get("name") == "graph.end" for e in _submitted_events(fake_client)), (
        "graph.end span carried no terminal-summary event"
    )
