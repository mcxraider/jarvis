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
from agents.agent_api.app.tracing import name_current_run
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
    }
    assert expected.issubset(node_names), (
        f"Missing nodes: {expected - node_names}"
    )

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
