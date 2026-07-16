"""Tests for selected-tool enforcement in the tools node."""

import asyncio
import json
import os

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from langchain_core.tools import tool

from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.dispatcher import ToolDispatcher


@tool
def allowed_lookup() -> str:
    """A harmless lookup tool used only by this test."""

    return "ok"


def _dispatcher(calls):
    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="allowed_lookup",
                openai_schema={"type": "function", "function": {"name": "allowed_lookup"}},
                handler=lambda _args: calls.append("allowed_lookup") or {"ok": True},
            ),
            ToolSpec(
                name="blocked_lookup",
                openai_schema={"type": "function", "function": {"name": "blocked_lookup"}},
                handler=lambda _args: calls.append("blocked_lookup") or {"blocked": False},
            ),
        ],
        langchain_builder=lambda dispatch: [allowed_lookup],
    )
    return ToolDispatcher(registry)


def _tool_call(name, call_id):
    return {
        "id": call_id,
        "function": {"name": name, "arguments": "{}"},
    }


def test_tools_node_rejects_tool_not_selected_for_turn():
    calls = []
    node = create_tools_node(_dispatcher(calls))
    state = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [_tool_call("blocked_lookup", "c1")],
            }
        ],
        "tool_results": [],
        "selected_tool_names": ["allowed_lookup"],
    }

    result = asyncio.run(node(state))

    assert calls == []
    tool_result = result["tool_results"][0]
    assert tool_result["success"] is False
    assert tool_result["out_of_route_tool"] is True
    payload = json.loads(result["messages"][-1]["content"])
    assert payload["tool_name"] == "blocked_lookup"
    assert payload["out_of_route_tool"] is True


def test_tools_node_preserves_original_order_across_executed_and_rejected_calls():
    calls = []
    node = create_tools_node(_dispatcher(calls))
    state = {
        "thread_id": "thread-1",
        "turn_count": 4,
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    _tool_call("allowed_lookup", "c1"),
                    _tool_call("blocked_lookup", "c2"),
                    _tool_call("allowed_lookup", "c3"),
                ],
            }
        ],
        "tool_results": [],
        "selected_tool_names": ["allowed_lookup"],
    }

    result = asyncio.run(node(state))

    assert calls == ["allowed_lookup", "allowed_lookup"]
    assert [item["tool_call_id"] for item in result["tool_results"]] == [
        "c1",
        "c2",
        "c3",
    ]
    assert result["tool_results"][1]["out_of_route_tool"] is True
    assert [json.loads(message["content"])["tool_call_id"] for message in result["messages"][1:]] == [
        "c1",
        "c2",
        "c3",
    ]
