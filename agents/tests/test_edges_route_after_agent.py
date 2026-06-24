"""Tests for the route_after_agent routing function."""

import json

import pytest

from agents.agent_api.app.graph.edges import route_after_agent
from agents.agent_api.app.graph.risk import BULK_THRESHOLD


def make_tool_call(name: str, args=None) -> dict:
    """Build a minimal tool_call dict matching the OpenAI/DeepSeek format."""
    return {"id": f"call_{name}", "function": {"name": name, "arguments": json.dumps(args or {})}}


def _state_with_tool_calls(tool_calls: list, **extra) -> dict:
    """Build a JarvisState-shaped dict with one assistant message carrying tool_calls."""
    return {"messages": [{"role": "assistant", "tool_calls": tool_calls}], **extra}


class TestRouteAfterAgent:
    def test_no_tool_calls_routes_to_end(self):
        state = {"messages": [{"role": "assistant", "tool_calls": []}]}
        assert route_after_agent(state) == "end"

    def test_error_in_state_routes_to_end(self):
        # Even if tool_calls are present, an error short-circuits to "end".
        tool_calls = [make_tool_call("add_todoist_task")]
        state = _state_with_tool_calls(tool_calls, error="something went wrong")
        assert route_after_agent(state) == "end"

    def test_ask_user_routes_to_hitl(self):
        tool_calls = [make_tool_call("ask_user", {"question": "Which project?"})]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "hitl"

    def test_safe_only_routes_to_tools(self):
        # "get_tasks" is a read-only tool, not in RISKY or MUTATING sets.
        tool_calls = [make_tool_call("get_tasks")]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "tools"

    def test_risky_routes_to_confirm(self):
        tool_calls = [make_tool_call("delete_todoist_task", {"task_id": "123"})]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "confirm"

    def test_mixed_risky_and_safe_routes_to_confirm(self):
        tool_calls = [
            make_tool_call("delete_todoist_task", {"task_id": "123"}),
            make_tool_call("get_tasks"),
        ]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "confirm"

    def test_ask_user_priority_over_risky(self):
        # ask_user check happens before partition_tool_calls, so it wins.
        tool_calls = [
            make_tool_call("ask_user", {"question": "Are you sure?"}),
            make_tool_call("delete_todoist_task", {"task_id": "456"}),
        ]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "hitl"

    def test_empty_messages_routes_to_end(self):
        state = {"messages": []}
        assert route_after_agent(state) == "end"

    def test_latest_message_no_tool_calls_key_routes_to_end(self):
        # Message dict exists but lacks a tool_calls key entirely.
        state = {"messages": [{"role": "assistant", "content": "Done!"}]}
        assert route_after_agent(state) == "end"

    def test_mutating_below_bulk_threshold_routes_to_tools(self):
        # A single add_todoist_task with no prior mutations is "low" risk -> tools.
        tool_calls = [make_tool_call("add_todoist_task", {"content": "Buy milk"})]
        state = _state_with_tool_calls(tool_calls, tool_results=[])
        assert route_after_agent(state) == "tools"

    def test_mutating_at_bulk_threshold_routes_to_confirm(self):
        # When prior mutation count meets the bulk threshold, mutating calls become risky.
        prior_results = [
            {"tool_name": "add_todoist_task"} for _ in range(BULK_THRESHOLD)
        ]
        tool_calls = [make_tool_call("add_todoist_task", {"content": "Another task"})]
        state = _state_with_tool_calls(tool_calls, tool_results=prior_results)
        assert route_after_agent(state) == "confirm"
