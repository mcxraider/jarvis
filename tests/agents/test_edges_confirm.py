"""Tests for routing edges related to the confirm flow."""

import pytest

from agents.agent_api.app.graph.edges import route_after_agent, route_after_confirm


def _make_tool_call(name: str, call_id: str = "call_1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}


class TestRouteAfterAgent:
    def test_error_returns_end(self):
        state = {"error": "something broke", "messages": []}
        assert route_after_agent(state) == "end"

    def test_no_tool_calls_returns_end(self):
        state = {"messages": [{"role": "assistant", "content": "Done."}]}
        assert route_after_agent(state) == "end"

    def test_empty_messages_returns_end(self):
        state = {"messages": []}
        assert route_after_agent(state) == "end"

    def test_ask_user_returns_hitl(self):
        state = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [_make_tool_call("ask_user")],
                }
            ]
        }
        assert route_after_agent(state) == "hitl"

    def test_safe_mutation_routes_to_validate(self):
        state = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [_make_tool_call("add_todoist_task")],
                }
            ],
            "tool_results": [],
        }
        assert route_after_agent(state) == "validate"

    def test_read_tool_routes_to_validate(self):
        state = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [_make_tool_call("get_todoist_tasks")],
                }
            ],
            "tool_results": [],
        }
        assert route_after_agent(state) == "validate"

    def test_risky_tool_routes_to_validate(self):
        state = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [_make_tool_call("delete_todoist_task")],
                }
            ],
            "tool_results": [],
        }
        assert route_after_agent(state) == "validate"

    def test_mixed_risky_and_safe_routes_to_validate(self):
        state = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        _make_tool_call("add_todoist_task", "c1"),
                        _make_tool_call("delete_todoist_task", "c2"),
                    ],
                }
            ],
            "tool_results": [],
        }
        assert route_after_agent(state) == "validate"


class TestRouteAfterConfirm:
    def test_approve(self):
        state = {"confirm_decision": "approve"}
        assert route_after_confirm(state) == "approve"

    def test_decline(self):
        state = {"confirm_decision": "decline"}
        assert route_after_confirm(state) == "decline"

    def test_missing_defaults_to_decline(self):
        state = {}
        assert route_after_confirm(state) == "decline"

    def test_none_defaults_to_decline(self):
        state = {"confirm_decision": None}
        assert route_after_confirm(state) == "decline"
