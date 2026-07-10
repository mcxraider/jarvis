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

    def test_single_mutation_is_low_regardless_of_history(self):
        results = [
            {"tool_name": "add_todoist_task"} for _ in range(BULK_THRESHOLD + 5)
        ]
        state = _make_state(results)
        tc = _make_tool_call("add_todoist_task")
        assert classify_risk(tc, state) == "low"

    def test_read_unaffected_by_history(self):
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

    def test_batch_of_5_mutations_all_risky(self):
        calls = [_make_tool_call("add_todoist_task", f"c{i}") for i in range(5)]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert len(risky) == 5
        assert safe == []

    def test_batch_of_10_mixed_mutations_all_risky(self):
        calls = [
            _make_tool_call("add_todoist_task", "c0"),
            _make_tool_call("update_todoist_task", "c1"),
            _make_tool_call("complete_task", "c2"),
            _make_tool_call("create_calendar_event", "c3"),
            _make_tool_call("update_calendar_event", "c4"),
            _make_tool_call("add_todoist_task", "c5"),
            _make_tool_call("add_todoist_task", "c6"),
            _make_tool_call("create_calendar_event", "c7"),
            _make_tool_call("add_todoist_task", "c8"),
            _make_tool_call("add_todoist_task", "c9"),
        ]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert len(risky) == 10
        assert safe == []

    def test_batch_of_4_mutations_stays_safe(self):
        calls = [_make_tool_call("add_todoist_task", f"c{i}") for i in range(4)]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert risky == []
        assert len(safe) == 4

    def test_batch_exactly_at_threshold(self):
        calls = [_make_tool_call("add_todoist_task", f"c{i}") for i in range(BULK_THRESHOLD)]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert len(risky) == BULK_THRESHOLD
        assert safe == []

    def test_historical_mutations_do_not_gate_small_batch(self):
        results = [{"tool_name": "add_todoist_task"} for _ in range(10)]
        state = _make_state(results)
        calls = [_make_tool_call("add_todoist_task", "c1")]
        risky, safe = partition_tool_calls(calls, state)
        assert risky == []
        assert len(safe) == 1

    def test_reads_stay_safe_in_bulk_batch(self):
        calls = [
            _make_tool_call("add_todoist_task", f"c{i}") for i in range(5)
        ] + [_make_tool_call("get_todoist_tasks", "r1")]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert len(risky) == 5
        assert len(safe) == 1
        assert safe[0]["id"] == "r1"

    def test_delete_risky_in_small_batch(self):
        calls = [_make_tool_call("delete_todoist_task", "c1")]
        risky, safe = partition_tool_calls(calls, _make_state())
        assert len(risky) == 1
        assert safe == []
