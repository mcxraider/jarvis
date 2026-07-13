"""Tests for the v1 Todoist tools added/enriched from todoist_tools_complete.md.

Covers the four new REST-backed tools (uncomplete_task, get_comments, add_comment,
get_labels), the schema/handler/langchain consistency invariant, and the corrected
priority description.
"""

from unittest.mock import patch

import pytest

from agents.agent_api.app.tools.todoist.client import TODOIST_REST_BASE_URL, TodoistApiClient
from agents.agent_api.app.tools.todoist.schemas import (
    MUTATING_TOOL_NAMES,
    get_todoist_tool_schemas,
)
from agents.agent_api.app.tools.todoist.tools import (
    TODOIST_PROMPT_FRAGMENT,
    build_todoist_langchain_tools,
    get_todoist_tool_specs,
)

NEW_TOOLS = {"uncomplete_task", "get_comments", "add_comment", "get_labels"}
PROJECT_TOOLS = {"get_projects", "create_project"}


def _schema(name):
    return next(s for s in get_todoist_tool_schemas() if s["function"]["name"] == name)


class TestSchemaRegistration:
    def test_new_tools_registered(self):
        names = {s["function"]["name"] for s in get_todoist_tool_schemas()}
        assert NEW_TOOLS <= names

    def test_project_tools_registered(self):
        names = {s["function"]["name"] for s in get_todoist_tool_schemas()}
        assert PROJECT_TOOLS <= names

    def test_mutating_membership(self):
        # New writers must be guarded; new readers must not be.
        assert "uncomplete_task" in MUTATING_TOOL_NAMES
        assert "add_comment" in MUTATING_TOOL_NAMES
        assert "get_comments" not in MUTATING_TOOL_NAMES
        assert "get_labels" not in MUTATING_TOOL_NAMES

    def test_project_mutating_membership(self):
        # create_project writes; get_projects is a read.
        assert "create_project" in MUTATING_TOOL_NAMES
        assert "get_projects" not in MUTATING_TOOL_NAMES

    def test_create_project_requires_name(self):
        params = _schema("create_project")["function"]["parameters"]
        assert params["required"] == ["name"]

    def test_add_comment_requires_content(self):
        params = _schema("add_comment")["function"]["parameters"]
        assert params["required"] == ["content"]
        assert "task_id" in params["properties"]
        assert "project_id" in params["properties"]

    def test_priority_description_corrected(self):
        # The old text ("1 normal, 2 low, 3 medium, 4 high") was inconsistent.
        desc = _schema("add_todoist_task")["function"]["parameters"]["properties"]["priority"][
            "description"
        ]
        assert "highest urgency" in desc
        assert "1 normal, 2 low" not in desc

    def test_priority_prompt_matches_add_and_update_schema(self):
        expected = "4 = urgent/P1, 3 = high/P2, 2 = medium/P3, 1 = normal/P4 (default)"

        assert expected in TODOIST_PROMPT_FRAGMENT
        for tool_name in ("add_todoist_task", "update_todoist_task"):
            description = _schema(tool_name)["function"]["parameters"]["properties"][
                "priority"
            ]["description"]
            assert "4 = highest urgency" in description
            assert "3 = P2, 2 = P3, 1 = normal/default (P4)" in description


class TestLayerConsistency:
    """schema names == handler-map names == langchain tool names (no silent drift)."""

    def test_three_layers_agree(self):
        client = TodoistApiClient(api_key="test-key")
        schema_names = {s["function"]["name"] for s in get_todoist_tool_schemas()}
        spec_names = {spec.name for spec in get_todoist_tool_specs(client)}
        lc_names = {t.name for t in build_todoist_langchain_tools(lambda *a, **k: {})}
        assert schema_names == spec_names == lc_names


class TestUpdateTaskWrapper:
    @staticmethod
    def _invoke(arguments):
        calls = []

        def dispatch(*args):
            calls.append(args)
            return {"success": True}

        tool = next(
            tool
            for tool in build_todoist_langchain_tools(dispatch)
            if tool.name == "update_todoist_task"
        )
        tool.invoke(
            {
                "args": arguments,
                "name": "update_todoist_task",
                "type": "tool_call",
                "id": "call-update",
            }
        )
        return calls[-1][2]

    def test_priority_only_omits_optional_defaults(self):
        assert self._invoke({"task_id": "task-1", "priority": 4}) == {
            "task_id": "task-1",
            "priority": 4,
        }

    def test_priority_and_duration_update_passes_only_supplied(self):
        # Mirrors the real failing request ("... 1130 to 2pm p1"): must not raise and
        # must forward exactly the supplied fields, leaving clearable fields
        # (assignee_id/deadline_date) out so they are not wiped.
        assert self._invoke(
            {
                "task_id": "task-1",
                "priority": 4,
                "duration": 150,
                "duration_unit": "minute",
            }
        ) == {
            "task_id": "task-1",
            "priority": 4,
            "duration": 150,
            "duration_unit": "minute",
        }

    def test_explicit_null_is_preserved(self):
        assert self._invoke({"task_id": "task-1", "assignee_id": None}) == {
            "task_id": "task-1",
            "assignee_id": None,
        }

    def test_explicit_null_labels_is_rejected(self):
        with pytest.raises(ValueError, match="labels"):
            self._invoke({"task_id": "task-1", "labels": None})


class TestUncompleteTask:
    @patch.object(TodoistApiClient, "_request")
    def test_posts_reopen(self, mock_request):
        mock_request.return_value = None
        client = TodoistApiClient(api_key="test-key")

        result = client.uncomplete_task({"task_id": "42"})

        url, method = mock_request.call_args[0][0], mock_request.call_args[0][1]
        assert url == f"{TODOIST_REST_BASE_URL}/tasks/42/reopen"
        assert method == "POST"
        assert result["success"] is True


class TestGetComments:
    @patch.object(TodoistApiClient, "_request")
    def test_single_comment_by_id(self, mock_request):
        mock_request.return_value = {"id": "c1"}
        client = TodoistApiClient(api_key="test-key")

        client.get_comments({"comment_id": "c1"})

        assert mock_request.call_args[0][0] == f"{TODOIST_REST_BASE_URL}/comments/c1"

    @patch.object(TodoistApiClient, "_request")
    def test_list_by_task(self, mock_request):
        mock_request.return_value = {"results": []}
        client = TodoistApiClient(api_key="test-key")

        client.get_comments({"task_id": "99"})

        assert "task_id=99" in mock_request.call_args[0][0]

    def test_requires_a_target(self):
        client = TodoistApiClient(api_key="test-key")
        with pytest.raises(ValueError):
            client.get_comments({})


class TestAddComment:
    @patch.object(TodoistApiClient, "_request")
    def test_posts_payload(self, mock_request):
        mock_request.return_value = {"id": "c2"}
        client = TodoistApiClient(api_key="test-key")

        client.add_comment({"content": "hi", "task_id": "7"})

        url, method, payload = mock_request.call_args[0][:3]
        assert url == f"{TODOIST_REST_BASE_URL}/comments"
        assert method == "POST"
        assert payload == {"content": "hi", "task_id": "7"}

    def test_rejects_both_targets(self):
        client = TodoistApiClient(api_key="test-key")
        with pytest.raises(ValueError):
            client.add_comment({"content": "hi", "task_id": "7", "project_id": "8"})

    def test_rejects_no_target(self):
        client = TodoistApiClient(api_key="test-key")
        with pytest.raises(ValueError):
            client.add_comment({"content": "hi"})


class TestGetLabels:
    @patch.object(TodoistApiClient, "_request")
    def test_no_search_returns_raw(self, mock_request):
        payload = {"results": [{"name": "work"}, {"name": "home"}], "next_cursor": None}
        mock_request.return_value = payload
        client = TodoistApiClient(api_key="test-key")

        assert client.get_labels({}) == payload

    @patch.object(TodoistApiClient, "_request")
    def test_search_filters_client_side(self, mock_request):
        mock_request.return_value = {
            "results": [{"name": "Work"}, {"name": "homework"}, {"name": "home"}],
            "next_cursor": "x",
        }
        client = TodoistApiClient(api_key="test-key")

        result = client.get_labels({"search": "work"})

        names = [label["name"] for label in result["results"]]
        assert names == ["Work", "homework"]  # case-insensitive substring
        assert result["next_cursor"] == "x"  # preserves other response fields
        # search must not be forwarded to the API as a query param
        assert "search" not in mock_request.call_args[0][0]


class TestGetProjects:
    @patch.object(TodoistApiClient, "_request")
    def test_no_search_returns_raw(self, mock_request):
        payload = {"results": [{"id": "1", "name": "Inbox"}], "next_cursor": None}
        mock_request.return_value = payload
        client = TodoistApiClient(api_key="test-key")

        assert client.get_projects({}) == payload
        assert mock_request.call_args[0][0] == f"{TODOIST_REST_BASE_URL}/projects"

    @patch.object(TodoistApiClient, "_request")
    def test_search_filters_client_side(self, mock_request):
        mock_request.return_value = {
            "results": [
                {"id": "1", "name": "jarvis-mcp"},
                {"id": "2", "name": "Inbox"},
                {"id": "3", "name": "MCP notes"},
            ],
            "next_cursor": "y",
        }
        client = TodoistApiClient(api_key="test-key")

        result = client.get_projects({"search": "mcp"})

        names = [project["name"] for project in result["results"]]
        assert names == ["jarvis-mcp", "MCP notes"]  # case-insensitive substring
        assert result["next_cursor"] == "y"  # preserves other response fields
        # search must not be forwarded to the API as a query param
        assert "search" not in mock_request.call_args[0][0]


class TestCreateProject:
    @patch.object(TodoistApiClient, "_request")
    def test_posts_payload(self, mock_request):
        mock_request.return_value = {"id": "p1", "name": "Reading"}
        client = TodoistApiClient(api_key="test-key")

        client.create_project({"name": "Reading", "color": None})

        url, method, payload = mock_request.call_args[0][:3]
        assert url == f"{TODOIST_REST_BASE_URL}/projects"
        assert method == "POST"
        # None-valued fields are dropped before sending.
        assert payload == {"name": "Reading"}
