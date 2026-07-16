"""Tests for the validate_entities node (logic only; not yet wired into the graph)."""

import asyncio
import json
from unittest.mock import patch

from agents.agent_api.app.graph.nodes.validate_entities import create_validate_entities_node
from agents.agent_api.app.tools.metadata import EntityRef


def _tool_call(name: str, call_id: str = "call_1", args=None) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args or {})}}


def _result(content, tool_name: str = "get_tasks", success: bool = True) -> dict:
    return {
        "tool_call_id": "read_call",
        "tool_name": tool_name,
        "success": success,
        "content": content,
        "error": None,
        "mutation_blocked": False,
        "classified_error": None,
    }


def _state(tool_calls, tool_results=None, selected_tool_names=None) -> dict:
    return {
        "messages": [{"role": "assistant", "tool_calls": tool_calls}],
        "tool_results": tool_results or [],
        "thread_id": "thread_test",
        "turn_count": 1,
        **({"selected_tool_names": selected_tool_names} if selected_tool_names is not None else {}),
    }


def _node():
    return create_validate_entities_node()


def _run(state):
    return asyncio.run(_node()(state))


class TestPassthrough:
    def test_read_only_routes_to_tools_without_touching_messages(self):
        result = _run(_state([_tool_call("get_tasks")]))
        assert result == {"next": "tools"}

    def test_low_mutation_with_seen_id_routes_to_tools(self):
        state = _state(
            [_tool_call("complete_task", args={"task_id": "t1"})],
            tool_results=[_result([{"id": "t1"}])],
        )
        assert _run(state) == {"next": "tools"}

    def test_risky_mutation_with_seen_id_routes_to_confirm(self):
        state = _state(
            [_tool_call("delete_todoist_task", args={"task_id": "t1"})],
            tool_results=[_result([{"id": "t1"}])],
        )
        assert _run(state) == {"next": "prepare_confirm"}

    def test_fail_open_for_tools_without_requirements(self):
        # add_todoist_task has no entity requirements -> passes even with no reads.
        assert _run(_state([_tool_call("add_todoist_task", args={"content": "x"})])) == {
            "next": "tools"
        }


class TestBlocking:
    def test_out_of_route_tool_blocks_before_execution_routing(self):
        result = _run(
            _state(
                [_tool_call("get_tasks", "c1")],
                selected_tool_names=["ask_user", "list_calendar_events"],
            )
        )

        assert result["next"] == "agent"
        payload = json.loads(result["messages"][-1]["content"])
        assert payload["success"] is False
        assert payload["out_of_route_tool"] is True
        assert "get_tasks" in payload["error"]
        assert "list_calendar_events" in payload["error"]

    def test_out_of_route_tool_blocks_whole_batch(self):
        calls = [
            _tool_call("list_calendar_events", "c_allowed"),
            _tool_call("get_tasks", "c_rejected"),
        ]

        result = _run(
            _state(
                calls,
                selected_tool_names=["ask_user", "list_calendar_events"],
            )
        )

        assert result["next"] == "agent"
        tool_messages = [m for m in result["messages"] if m.get("role") == "tool"]
        by_id = {m["tool_call_id"]: json.loads(m["content"]) for m in tool_messages}
        assert by_id["c_rejected"]["out_of_route_tool"] is True
        assert by_id["c_allowed"]["deferred_for_clarification"] is True

    def test_unseen_id_blocks_and_loops_to_agent(self):
        result = _run(_state([_tool_call("complete_task", "c1", {"task_id": "ghost"})]))
        assert result["next"] == "agent"
        assert "tool_results" not in result  # blocked calls never enter the seen-index
        blocked = result["messages"][-1]
        assert blocked["role"] == "tool"
        assert blocked["tool_call_id"] == "c1"
        payload = json.loads(blocked["content"])
        assert payload["success"] is False
        assert payload["unverified_entity"] is True
        assert "ghost" in payload["error"]

    def test_whole_batch_blocked_when_one_call_is_unverified(self):
        calls = [
            _tool_call("get_tasks", "c_read"),
            _tool_call("complete_task", "c_mut", {"task_id": "ghost"}),
        ]
        result = _run(_state(calls))
        assert result["next"] == "agent"
        tool_messages = [m for m in result["messages"] if m.get("role") == "tool"]
        assert len(tool_messages) == 2  # one synthetic result per call in the batch
        by_id = {m["tool_call_id"]: json.loads(m["content"]) for m in tool_messages}
        assert by_id["c_mut"]["unverified_entity"] is True
        assert by_id["c_read"]["deferred_for_clarification"] is True

    def test_same_turn_read_does_not_satisfy_mutation(self):
        # Read + mutation emitted together: the read isn't in tool_results yet, so the
        # mutation's id is unseen and the batch is blocked.
        calls = [
            _tool_call("get_tasks", "c_read"),
            _tool_call("complete_task", "c_mut", {"task_id": "t1"}),
        ]
        result = _run(_state(calls, tool_results=[]))
        assert result["next"] == "agent"
        mut_msg = next(
            json.loads(m["content"])
            for m in result["messages"]
            if m.get("role") == "tool" and m["tool_call_id"] == "c_mut"
        )
        assert mut_msg["unverified_entity"] is True

    def test_empty_tool_calls_routes_to_agent(self):
        assert _run(_state([])) == {"next": "agent"}

    def test_multi_ref_violation_produces_single_message_with_both_args(self):
        # When a tool call has two entity refs that both violate, _unverified_message
        # must receive ALL violations for that call_id and report both args in one
        # error string — not silently drop the first.
        two_refs = (
            EntityRef("task_id", "task"),
            EntityRef("project_id", "project"),
        )
        call = _tool_call("complete_task", "c1", {"task_id": "ghost_t", "project_id": "ghost_p"})
        with patch(
            "agents.agent_api.app.graph.entity_index.entity_requirements",
            return_value=two_refs,
        ):
            result = _run(_state([call]))

        assert result["next"] == "agent"
        tool_messages = [m for m in result["messages"] if m.get("role") == "tool"]
        # Exactly one synthetic message for the one tool call.
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "c1"
        payload = json.loads(tool_messages[0]["content"])
        assert payload["success"] is False
        assert payload["unverified_entity"] is True
        # Both arg names must appear in the single error string.
        assert "task_id" in payload["error"]
        assert "project_id" in payload["error"]
