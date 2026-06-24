"""Tests for route_after_tools edge — routing decisions based on tool result size."""

import json

from agents.agent_api.app.graph.edges import route_after_tools


def _make_tool_result(content, tool_name="get_tasks"):
    return {
        "tool_call_id": "call_1",
        "tool_name": tool_name,
        "success": True,
        "content": content,
        "error": None,
    }


def _make_tool_message(content, tool_name="get_tasks"):
    envelope = _make_tool_result(content, tool_name)
    return {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": tool_name,
        "content": json.dumps(envelope, default=str),
    }


def _make_state(items, tool_name="get_tasks"):
    """Build minimal state with tool results and matching messages."""
    return {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "call_1"}]},
            _make_tool_message(items, tool_name),
        ],
        "tool_results": [_make_tool_result(items, tool_name)],
    }


class TestRouteAfterTools:
    def test_routes_to_agent_when_no_tool_results(self):
        state = {"messages": [], "tool_results": []}
        assert route_after_tools(state) == "agent"

    def test_routes_to_agent_when_under_threshold(self):
        items = [{"id": str(i)} for i in range(10)]
        state = _make_state(items)
        assert route_after_tools(state) == "agent"

    def test_routes_to_summarize_when_over_threshold(self):
        items = [{"id": str(i)} for i in range(55)]
        state = _make_state(items)
        assert route_after_tools(state) == "summarize"

    def test_handles_items_key(self):
        content = {"items": [{"id": str(i)} for i in range(55)]}
        state = _make_state(content)
        assert route_after_tools(state) == "summarize"

    def test_handles_results_key(self):
        content = {"results": [{"id": str(i)} for i in range(55)]}
        state = _make_state(content)
        assert route_after_tools(state) == "summarize"

    def test_handles_tasks_key(self):
        content = {"tasks": [{"id": str(i)} for i in range(55)]}
        state = _make_state(content)
        assert route_after_tools(state) == "summarize"

    def test_routes_agent_for_non_list_content(self):
        content = {"success": True, "message": "Task completed"}
        state = _make_state(content)
        assert route_after_tools(state) == "agent"

    def test_exactly_at_threshold_routes_to_agent(self):
        items = [{"id": str(i)} for i in range(50)]
        state = _make_state(items)
        assert route_after_tools(state) == "agent"
