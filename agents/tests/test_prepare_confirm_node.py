"""Tests for the prepare_confirm node."""

import json

import pytest

from agents.agent_api.app.graph.nodes.prepare_confirm import create_prepare_confirm_node


def _make_tool_call(name: str, call_id: str = "call_1", args=None) -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


def _make_state(tool_calls, tool_results=None) -> dict:
    return {
        "messages": [{"role": "assistant", "tool_calls": tool_calls}],
        "tool_results": tool_results or [],
        "thread_id": "thread_test",
        "turn_count": 3,
    }


class TestPrepareConfirmNode:
    def test_freezes_risky_call(self):
        calls = [_make_tool_call("delete_todoist_task", "c1", {"task_id": "99"})]
        state = _make_state(calls)
        node = create_prepare_confirm_node()
        result = node(state)
        held_calls = result["held_calls"]
        assert len(held_calls) == 1
        assert held_calls[0]["tool_name"] == "delete_todoist_task"
        assert held_calls[0]["args"] == {"task_id": "99"}
        assert held_calls[0]["origin_tool_call_id"] == "c1"
        assert result["pending_interrupt"] == "confirm"

    def test_defers_safe_sibling_calls(self):
        calls = [
            _make_tool_call("delete_todoist_task", "c1", {"task_id": "1"}),
            _make_tool_call("add_todoist_task", "c2", {"content": "x"}),
            _make_tool_call("get_todoist_tasks", "c3"),
        ]
        state = _make_state(calls)
        node = create_prepare_confirm_node()
        result = node(state)
        # All risky calls held, safe calls deferred
        assert len(result["held_calls"]) == 1
        messages = result["messages"]
        deferred = [m for m in messages if m.get("role") == "tool"]
        assert len(deferred) == 2
        for msg in deferred:
            content = json.loads(msg["content"])
            assert content["success"] is False
            assert "Deferred" in content["error"]

    def test_freezes_all_risky_calls_as_batch(self):
        calls = [
            _make_tool_call("delete_todoist_task", "c1", {"task_id": "1"}),
            _make_tool_call("delete_todoist_task", "c2", {"task_id": "2"}),
        ]
        state = _make_state(calls)
        node = create_prepare_confirm_node()
        result = node(state)
        held_calls = result["held_calls"]
        assert len(held_calls) == 2
        assert held_calls[0]["origin_tool_call_id"] == "c1"
        assert held_calls[1]["origin_tool_call_id"] == "c2"
        # No deferred messages — all risky calls are held, no safe calls
        deferred = [m for m in result["messages"] if m.get("role") == "tool"]
        assert len(deferred) == 0

    def test_no_risky_calls_errors(self):
        calls = [_make_tool_call("get_todoist_tasks", "c1")]
        state = _make_state(calls)
        node = create_prepare_confirm_node()
        result = node(state)
        assert result.get("error")
        assert result["next"] == "end"

    def test_empty_tool_calls_errors(self):
        state = _make_state([])
        node = create_prepare_confirm_node()
        result = node(state)
        assert result.get("error")
