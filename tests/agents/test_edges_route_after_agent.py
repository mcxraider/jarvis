"""Tests for the route_after_agent routing function.

Since prior-read ID validation landed, ``route_after_agent`` no longer performs the
risk split itself: any non-ask_user tool calls route to the ``validate_entities`` node,
which verifies entity IDs and *then* decides tools-vs-confirm. The tools/confirm split
is covered by ``test_validate_entities_node.py``.
"""

import json

from agents.agent_api.app.graph.edges import route_after_agent


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

    def test_safe_tool_calls_route_to_validate(self):
        # Read-only tools still go through validation first (a fast no-op there).
        tool_calls = [make_tool_call("get_tasks")]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "validate"

    def test_risky_tool_calls_route_to_validate(self):
        tool_calls = [make_tool_call("delete_todoist_task", {"task_id": "123"})]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "validate"

    def test_mixed_tool_calls_route_to_validate(self):
        tool_calls = [
            make_tool_call("delete_todoist_task", {"task_id": "123"}),
            make_tool_call("get_tasks"),
        ]
        state = _state_with_tool_calls(tool_calls)
        assert route_after_agent(state) == "validate"

    def test_ask_user_priority_over_other_calls(self):
        # ask_user check happens before the validate branch, so it wins.
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
