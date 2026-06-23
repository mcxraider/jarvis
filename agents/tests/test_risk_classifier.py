"""Tests for the risk classifier module."""

import pytest

from agents.agent_api.app.graph.risk import (
    BULK_THRESHOLD,
    RISKY_TOOLS,
    classify_risk,
    partition_tool_calls,
)


def _make_tool_call(name: str, call_id: str = "call_1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}


def _make_state(tool_results=None) -> dict:
    return {"tool_results": tool_results or []}


class TestClassifyRisk:
    def test_delete_is_risky(self):
        tc = _make_tool_call("delete_todoist_task")
        assert classify_risk(tc, _make_state()) == "risky"

    def test_risky_tools_constant(self):
        assert "delete_todoist_task" in RISKY_TOOLS

    def test_add_task_is_low(self):
        tc = _make_tool_call("add_todoist_task")
        assert classify_risk(tc, _make_state()) == "low"

    def test_update_task_is_low(self):
        tc = _make_tool_call("update_todoist_task")
        assert classify_risk(tc, _make_state()) == "low"

    def test_complete_task_is_low(self):
        tc = _make_tool_call("complete_task")
        assert classify_risk(tc, _make_state()) == "low"

    def test_read_tool_is_read(self):
        tc = _make_tool_call("get_todoist_tasks")
        assert classify_risk(tc, _make_state()) == "read"

    def test_unknown_tool_is_read(self):
        tc = _make_tool_call("some_random_tool")
        assert classify_risk(tc, _make_state()) == "read"

    def test_bulk_threshold_triggers_risky(self):
        results = [
            {"tool_name": "add_todoist_task"} for _ in range(BULK_THRESHOLD)
        ]
        state = _make_state(results)
        tc = _make_tool_call("add_todoist_task")
        assert classify_risk(tc, state) == "risky"

    def test_below_bulk_threshold_stays_low(self):
        results = [
            {"tool_name": "add_todoist_task"} for _ in range(BULK_THRESHOLD - 1)
        ]
        state = _make_state(results)
        tc = _make_tool_call("add_todoist_task")
        assert classify_risk(tc, state) == "low"

    def test_bulk_threshold_does_not_affect_reads(self):
        results = [
            {"tool_name": "add_todoist_task"} for _ in range(BULK_THRESHOLD + 5)
        ]
        state = _make_state(results)
        tc = _make_tool_call("get_todoist_tasks")
        assert classify_risk(tc, state) == "read"

    def test_delete_risky_regardless_of_count(self):
        state = _make_state([])
        tc = _make_tool_call("delete_todoist_task")
        assert classify_risk(tc, state) == "risky"


class TestPartitionToolCalls:
    def test_all_safe(self):
        calls = [_make_tool_call("add_todoist_task"), _make_tool_call("get_todoist_tasks")]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert risky == []
        assert len(safe) == 2

    def test_all_risky(self):
        calls = [_make_tool_call("delete_todoist_task", "c1"), _make_tool_call("delete_todoist_task", "c2")]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert len(risky) == 2
        assert safe == []

    def test_mixed(self):
        calls = [
            _make_tool_call("delete_todoist_task", "c1"),
            _make_tool_call("add_todoist_task", "c2"),
            _make_tool_call("get_todoist_tasks", "c3"),
        ]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert len(risky) == 1
        assert risky[0]["id"] == "c1"
        assert len(safe) == 2

    def test_empty_list(self):
        risky, safe = partition_tool_calls([], _make_state())
        assert risky == []
        assert safe == []
