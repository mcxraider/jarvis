"""Tests for the tool display metadata module."""

import pytest

from agents.agent_api.app.tools.metadata import (
    DEFAULT_META,
    ToolDisplayMeta,
    always_risky_tools,
    get_meta,
    get_service,
    get_verb,
    irreversible_tools,
)


class TestGetMeta:
    def test_known_tools(self):
        assert get_meta("delete_todoist_task").verb == "deleting"
        assert get_meta("add_todoist_task").verb == "adding"
        assert get_meta("update_todoist_task").verb == "updating"
        assert get_meta("complete_task").verb == "completing"

    def test_unknown_tool_returns_default(self):
        meta = get_meta("totally_unknown_tool")
        assert meta is DEFAULT_META
        assert meta.verb == "modifying"
        assert meta.label == "Modify item"

    def test_labels(self):
        assert get_meta("delete_todoist_task").label == "Delete task"
        assert get_meta("add_todoist_task").label == "Add task"
        assert get_meta("complete_task").label == "Complete task"

    def test_create_project_meta(self):
        meta = get_meta("create_project")
        assert meta.verb == "adding"
        assert meta.label == "Add project"
        assert meta.service == "todoist"
        assert meta.highlight_arg == "name"


class TestGetService:
    @pytest.mark.parametrize(
        "tool_name,expected",
        [
            ("delete_todoist_task", "todoist"),
            ("add_todoist_task", "todoist"),
            ("update_todoist_task", "todoist"),
            ("complete_task", "todoist"),
            ("delete_calendar_event", "google calendar"),
            ("create_calendar_event", "google calendar"),
            ("update_calendar_event", "google calendar"),
        ],
    )
    def test_service_mapping(self, tool_name, expected):
        assert get_service(tool_name) == expected

    def test_unknown_tool_has_no_service(self):
        assert get_service("totally_unknown_tool") == ""
        assert DEFAULT_META.service == ""


class TestGetVerb:
    @pytest.mark.parametrize(
        "tool_name,expected",
        [
            ("delete_todoist_task", "deleting"),
            ("add_todoist_task", "adding"),
            ("update_todoist_task", "updating"),
            ("complete_task", "completing"),
            ("unknown_tool", "modifying"),
        ],
    )
    def test_verb_mapping(self, tool_name, expected):
        assert get_verb(tool_name) == expected


class TestIrreversibleTools:
    def test_only_delete_is_irreversible(self):
        irrev = irreversible_tools()
        assert "delete_todoist_task" in irrev
        assert "add_todoist_task" not in irrev
        assert "update_todoist_task" not in irrev
        assert "complete_task" not in irrev
        assert "create_project" not in irrev


class TestAlwaysRiskyTools:
    def test_risky_set(self):
        risky = always_risky_tools()
        assert "delete_todoist_task" in risky
        assert "add_todoist_task" not in risky
        assert "update_todoist_task" not in risky
        assert "complete_task" not in risky
        assert "create_project" not in risky

    def test_matches_risk_module(self):
        from agents.agent_api.app.graph.risk import RISKY_TOOLS

        assert always_risky_tools() == RISKY_TOOLS


class TestToolDisplayMetaFrozen:
    def test_cannot_mutate(self):
        meta = get_meta("delete_todoist_task")
        with pytest.raises(Exception):
            meta.verb = "removing"
