"""Tests for the executor node — all 4 guards + success + decline paths."""

import json
from unittest.mock import MagicMock

import pytest

from agents.agent_api.app.graph.canonicalize import build_held_call
from agents.agent_api.app.graph.nodes.executor import create_executor_node


def _make_held_call(task_id="42"):
    tc = {"id": "call_1", "function": {"name": "delete_todoist_task", "arguments": json.dumps({"task_id": task_id})}}
    return build_held_call(tc, "thread_1", 1)


def _make_dispatcher(allow=True, result=None):
    dispatcher = MagicMock()
    dispatcher.allow_mutations = allow
    dispatcher.execute_tool.return_value = result or {
        "tool_call_id": "call_1",
        "tool_name": "delete_todoist_task",
        "success": True,
        "content": {"deleted": True},
        "error": None,
    }
    return dispatcher


def _make_state(held_call, decision="approve", consumed=None):
    return {
        "held_call": held_call,
        "confirm_decision": decision,
        "consumed_call_ids": consumed or [],
        "messages": [],
        "tool_results": [],
    }


class TestGuard0MutationsDisabled:
    def test_blocks_when_mutations_disabled(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher(allow=False)
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = node(state)
        assert result["held_call"] is None
        assert "mutations globally disabled" in result["messages"][-1]["content"]
        dispatcher.execute_tool.assert_not_called()


class TestGuard1Approval:
    def test_decline_produces_decline_message(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="decline")
        result = node(state)
        assert result["held_call"] is None
        msg_content = json.loads(result["messages"][-1]["content"])
        assert msg_content["user_declined"] is True
        assert msg_content["success"] is False
        dispatcher.execute_tool.assert_not_called()

    def test_none_decision_treated_as_decline(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision=None)
        result = node(state)
        msg_content = json.loads(result["messages"][-1]["content"])
        assert msg_content["user_declined"] is True
        dispatcher.execute_tool.assert_not_called()


class TestGuard2HashBinding:
    def test_tampered_args_blocked(self):
        held = _make_held_call()
        held["args"]["task_id"] = "TAMPERED"
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = node(state)
        assert "hash mismatch" in result["messages"][-1]["content"]
        dispatcher.execute_tool.assert_not_called()


class TestGuard3SingleUse:
    def test_replay_blocked(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve", consumed=[held["id"]])
        result = node(state)
        assert "already executed" in result["messages"][-1]["content"]
        dispatcher.execute_tool.assert_not_called()


class TestSuccessfulExecution:
    def test_executes_frozen_call(self):
        held = _make_held_call()
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = node(state)
        dispatcher.execute_tool.assert_called_once_with(
            held["origin_tool_call_id"],
            held["tool_name"],
            held["args"],
        )
        assert result["held_call"] is None
        assert result["confirm_decision"] is None
        assert result["consumed_call_ids"] == [held["id"]]
        assert result["next"] == "agent"
        assert len(result["messages"]) == 1
        assert len(result["tool_results"]) == 1

    def test_result_appended_to_tool_results(self):
        held = _make_held_call()
        expected_result = {
            "tool_call_id": "call_1",
            "tool_name": "delete_todoist_task",
            "success": True,
            "content": {"deleted": True},
            "error": None,
        }
        dispatcher = _make_dispatcher(result=expected_result)
        node = create_executor_node(dispatcher)
        state = _make_state(held, decision="approve")
        result = node(state)
        assert result["tool_results"][-1] == expected_result


class TestMissingHeldCall:
    def test_missing_held_call_errors(self):
        dispatcher = _make_dispatcher()
        node = create_executor_node(dispatcher)
        state = {"held_call": None, "confirm_decision": "approve", "consumed_call_ids": [], "messages": [], "tool_results": []}
        result = node(state)
        assert result.get("error")
        assert result["next"] == "end"
