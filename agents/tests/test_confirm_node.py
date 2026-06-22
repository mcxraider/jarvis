"""Tests for the confirm node."""

from unittest.mock import patch

import pytest

from agents.agent_api.app.graph.nodes.confirm import (
    APPROVE_TOKENS,
    create_confirm_node,
    parse_decision,
    render_action_summary,
)


class TestParseDecision:
    @pytest.mark.parametrize("token", list(APPROVE_TOKENS))
    def test_approve_tokens(self, token):
        assert parse_decision(token) == "approve"

    @pytest.mark.parametrize("token", ["Approve", "YES", "CONFIRM", "Ok", "Y"])
    def test_approve_case_insensitive(self, token):
        assert parse_decision(token) == "approve"

    def test_approve_with_whitespace(self):
        assert parse_decision("  yes  ") == "approve"

    def test_decline_unknown(self):
        assert parse_decision("no") == "decline"

    def test_decline_empty(self):
        assert parse_decision("") == "decline"

    def test_decline_random(self):
        assert parse_decision("maybe later") == "decline"


class TestRenderActionSummary:
    def test_delete_task(self):
        held = {"tool_name": "delete_todoist_task", "args": {"task_id": "42"}}
        result = render_action_summary(held)
        assert "42" in result
        assert "irreversible" in result.lower()

    def test_generic_tool(self):
        held = {"tool_name": "update_todoist_task", "args": {"task_id": "1", "content": "New"}}
        result = render_action_summary(held)
        assert "update_todoist_task" in result
        assert "task_id" in result

    def test_truncates_args(self):
        held = {"tool_name": "some_tool", "args": {f"key{i}": f"val{i}" for i in range(10)}}
        result = render_action_summary(held)
        # Only first 4 args shown
        assert result.count("=") <= 4


class TestConfirmNode:
    def test_missing_held_call(self):
        node = create_confirm_node()
        state = {"held_call": None, "messages": []}
        result = node(state)
        assert result.get("error")
        assert result.get("next") == "end"

    @patch("agents.agent_api.app.graph.nodes.confirm.interrupt")
    def test_approve_flow(self, mock_interrupt):
        mock_interrupt.return_value = "yes"
        node = create_confirm_node()
        state = {
            "held_call": {
                "id": "hc_1",
                "tool_name": "delete_todoist_task",
                "args": {"task_id": "42"},
            },
            "messages": [],
            "pending_interrupt": "confirm",
        }
        result = node(state)
        assert result["confirm_decision"] == "approve"
        assert result["pending_interrupt"] is None
        mock_interrupt.assert_called_once()
        payload = mock_interrupt.call_args[0][0]
        assert payload["type"] == "confirm"
        assert payload["held_call_id"] == "hc_1"
        assert payload["tool_name"] == "delete_todoist_task"

    @patch("agents.agent_api.app.graph.nodes.confirm.interrupt")
    def test_decline_flow(self, mock_interrupt):
        mock_interrupt.return_value = "no thanks"
        node = create_confirm_node()
        state = {
            "held_call": {
                "id": "hc_2",
                "tool_name": "delete_todoist_task",
                "args": {"task_id": "7"},
            },
            "messages": [],
            "pending_interrupt": "confirm",
        }
        result = node(state)
        assert result["confirm_decision"] == "decline"
        assert result["pending_interrupt"] is None
