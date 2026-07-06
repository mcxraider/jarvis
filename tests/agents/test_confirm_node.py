"""Tests for the confirm node."""

from unittest.mock import patch

import pytest

from agents.agent_api.app.graph.nodes.confirm import (
    APPROVE_TOKENS,
    create_confirm_node,
    parse_decision,
    render_action_summary,
    render_batch_summary,
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
        assert result.endswith(".")

    def test_update_tool(self):
        held = {"tool_name": "update_todoist_task", "args": {"task_id": "1", "content": "New"}}
        result = render_action_summary(held)
        assert "Update task" in result
        assert "id=1" in result
        assert "content" in result

    def test_update_tool_with_task_context(self):
        held = {
            "tool_name": "update_todoist_task",
            "args": {"task_id": "1", "due_string": "tomorrow 9am"},
            "context": {"task_content": "Submit expense report"},
        }
        result = render_action_summary(held)
        assert result == 'Update task "Submit expense report": due_string (todoist).'
        assert "id=1" not in result

    def test_calendar_delete_with_event_context(self):
        held = {
            "tool_name": "delete_calendar_event",
            "args": {"event_id": "evt_1"},
            "context": {"event_summary": "Team sync"},
        }
        result = render_action_summary(held)
        assert result == 'Delete event "Team sync" (google calendar).'
        assert "id=" not in result

    def test_calendar_delete_without_context_falls_back_to_id(self):
        held = {"tool_name": "delete_calendar_event", "args": {"event_id": "evt_1"}}
        result = render_action_summary(held)
        assert result == "Delete event (id=evt_1) (google calendar)."

    def test_calendar_update_with_event_context(self):
        held = {
            "tool_name": "update_calendar_event",
            "args": {"event_id": "evt_1", "location": "Room 4"},
            "context": {"event_summary": "Team sync"},
        }
        result = render_action_summary(held)
        assert result == 'Update event "Team sync": location (google calendar).'
        assert "id=" not in result

    def test_calendar_update_without_context_falls_back_to_id(self):
        held = {
            "tool_name": "update_calendar_event",
            "args": {"event_id": "evt_1", "location": "Room 4"},
        }
        result = render_action_summary(held)
        assert result == "Update event (id=evt_1): location (google calendar)."

    def test_truly_generic_tool(self):
        held = {"tool_name": "some_unknown_tool", "args": {"foo": "bar", "baz": 42}}
        result = render_action_summary(held)
        assert "some_unknown_tool" in result
        assert "foo" in result

    def test_truncates_args(self):
        held = {"tool_name": "some_tool", "args": {f"key{i}": f"val{i}" for i in range(10)}}
        result = render_action_summary(held)
        # Only first 4 args shown
        assert result.count("=") <= 4


class TestConfirmNode:
    def test_missing_held_calls(self):
        node = create_confirm_node()
        state = {"held_calls": None, "messages": []}
        result = node(state)
        assert result.get("error")
        assert result.get("next") == "end"

    @patch("agents.agent_api.app.graph.nodes.confirm.interrupt")
    def test_approve_flow(self, mock_interrupt):
        mock_interrupt.return_value = "yes"
        node = create_confirm_node()
        state = {
            "held_calls": [
                {
                    "id": "hc_1",
                    "tool_name": "delete_todoist_task",
                    "args": {"task_id": "42"},
                },
            ],
            "messages": [],
            "pending_interrupt": "confirm",
        }
        result = node(state)
        assert result["confirm_decision"] == "approve"
        assert result["pending_interrupt"] is None
        mock_interrupt.assert_called_once()
        payload = mock_interrupt.call_args[0][0]
        assert payload["type"] == "confirm"
        assert "hc_1" in payload["held_call_ids"]
        assert "delete_todoist_task" in payload["tool_names"]
        assert payload["services"] == ["todoist"]

    @patch("agents.agent_api.app.graph.nodes.confirm.interrupt")
    def test_decline_flow(self, mock_interrupt):
        mock_interrupt.return_value = "no thanks"
        node = create_confirm_node()
        state = {
            "held_calls": [
                {
                    "id": "hc_2",
                    "tool_name": "delete_todoist_task",
                    "args": {"task_id": "7"},
                },
            ],
            "messages": [],
            "pending_interrupt": "confirm",
        }
        result = node(state)
        assert result["confirm_decision"] == "decline"
        assert result["pending_interrupt"] is None
        assert result.get("final_response")

    @patch("agents.agent_api.app.graph.nodes.confirm.interrupt")
    def test_batch_summary(self, mock_interrupt):
        mock_interrupt.return_value = "yes"
        node = create_confirm_node()
        state = {
            "held_calls": [
                {"id": "hc_1", "tool_name": "delete_todoist_task", "args": {"task_id": "1"}},
                {"id": "hc_2", "tool_name": "delete_todoist_task", "args": {"task_id": "2"}},
                {"id": "hc_3", "tool_name": "delete_todoist_task", "args": {"task_id": "3"}},
            ],
            "messages": [],
            "pending_interrupt": "confirm",
        }
        result = node(state)
        assert result["confirm_decision"] == "approve"
        payload = mock_interrupt.call_args[0][0]
        assert payload["count"] == 3
        assert len(payload["held_call_ids"]) == 3
        assert "I'm deleting 3 items" in payload["summary"]
        assert "This action is irreversible." in payload["summary"]

    def test_migration_shim_singular_held_call(self):
        """Old-format state with singular held_call still works."""
        from unittest.mock import patch as _patch
        with _patch("agents.agent_api.app.graph.nodes.confirm.interrupt", return_value="yes"):
            node = create_confirm_node()
            state = {
                "held_calls": None,
                "held_call": {
                    "id": "hc_old",
                    "tool_name": "delete_todoist_task",
                    "args": {"task_id": "99"},
                },
                "messages": [],
                "pending_interrupt": "confirm",
            }
            result = node(state)
            assert result["confirm_decision"] == "approve"


class TestRenderBatchSummary:
    def test_delete_batch_uses_deleting_verb(self):
        held_calls = [
            {"tool_name": "delete_todoist_task", "args": {"task_id": "1"}},
            {"tool_name": "delete_todoist_task", "args": {"task_id": "2"}},
            {"tool_name": "delete_todoist_task", "args": {"task_id": "3"}},
        ]
        summary = render_batch_summary(held_calls)
        assert "I'm deleting 3 items:" in summary
        assert "This action is irreversible." in summary

    def test_add_batch_uses_adding_verb(self):
        held_calls = [
            {"tool_name": "add_todoist_task", "args": {"content": "Task A"}},
            {"tool_name": "add_todoist_task", "args": {"content": "Task B"}},
        ]
        summary = render_batch_summary(held_calls)
        assert "I'm adding 2 items:" in summary
        assert "Please confirm to proceed." in summary

    def test_update_batch_uses_updating_verb(self):
        held_calls = [
            {
                "tool_name": "update_todoist_task",
                "args": {"task_id": "1", "content": "X"},
                "context": {"task_content": "First task"},
            },
            {
                "tool_name": "update_todoist_task",
                "args": {"task_id": "2", "priority": 4},
                "context": {"task_content": "Second task"},
            },
        ]
        summary = render_batch_summary(held_calls)
        assert "I'm updating 2 items:" in summary
        assert 'Update task "First task": content (todoist).' in summary
        assert 'Update task "Second task": priority (todoist).' in summary
        assert "Please confirm to proceed." in summary

    def test_mixed_batch_uses_modifying(self):
        held_calls = [
            {"tool_name": "delete_todoist_task", "args": {"task_id": "1"}},
            {"tool_name": "update_todoist_task", "args": {"task_id": "2", "content": "Y"}},
        ]
        summary = render_batch_summary(held_calls)
        assert "I'm modifying 2 items" in summary
        assert "deleting" in summary
        assert "updating" in summary
        assert "Some of these actions are irreversible." in summary

    def test_single_delete_irreversible_suffix(self):
        held_calls = [
            {"tool_name": "delete_todoist_task", "args": {"task_id": "1"}},
        ]
        summary = render_batch_summary(held_calls)
        assert "This action is irreversible." in summary

    def test_single_add_confirm_suffix(self):
        held_calls = [
            {"tool_name": "add_todoist_task", "args": {"content": "New task"}},
        ]
        summary = render_batch_summary(held_calls)
        assert "Please confirm to proceed." in summary
        assert "irreversible" not in summary.lower()

    def test_mixed_service_batch_labels_each_service(self):
        held_calls = [
            {
                "tool_name": "delete_todoist_task",
                "args": {"task_id": "1"},
                "context": {"task_content": "Buy milk"},
            },
            {
                "tool_name": "delete_calendar_event",
                "args": {"event_id": "evt_1"},
                "context": {"event_summary": "Team sync"},
            },
        ]
        summary = render_batch_summary(held_calls)
        assert '(todoist)' in summary
        assert '(google calendar)' in summary
        assert 'Buy milk' in summary
        assert 'Team sync' in summary

    def test_complete_task_batch(self):
        held_calls = [
            {"tool_name": "complete_task", "args": {"task_id": "1"}, "context": {"task_content": "Buy milk"}},
            {"tool_name": "complete_task", "args": {"task_id": "2"}, "context": {"task_content": "Call mom"}},
        ]
        summary = render_batch_summary(held_calls)
        assert "I'm completing 2 items:" in summary
        assert "Buy milk" in summary
        assert "Call mom" in summary
