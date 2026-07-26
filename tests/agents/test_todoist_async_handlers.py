"""Native async Todoist handler registration and sync-parity tests."""

import asyncio
import copy
import inspect
from unittest.mock import AsyncMock, Mock

import pytest

from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tools.todoist.schemas import (
    MUTATING_TOOL_NAMES,
    get_todoist_tool_schemas,
)
from agents.agent_api.app.tools.todoist.tools import get_todoist_tool_specs
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


EXPLICIT_COMPLETION_RANGE = {
    "since": "2026-07-01T00:00:00Z",
    "until": "2026-07-15T00:00:00Z",
}


@pytest.mark.parametrize(
    ("operation", "arguments", "provider_response"),
    [
        (
            "add_todoist_task",
            {"content": "Async task", "priority": 4, "description": None},
            {"id": "task-new", "content": "Async task"},
        ),
        ("get_todoist_task", {"task_id": "task-1"}, {"id": "task-1"}),
        (
            "get_tasks",
            {"ids": ["task-1", "task-2"], "project_id": None},
            {"results": [{"id": "task-1"}]},
        ),
        (
            "get_tasks_by_filter",
            {"query": "today & overdue", "lang": "en"},
            {"results": [{"id": "task-2"}]},
        ),
        (
            "update_todoist_task",
            {
                "task_id": "task-1",
                "priority": 4,
                "duration": 30,
                "duration_unit": "minute",
                "description": None,
            },
            {"id": "task-1", "priority": 4},
        ),
        ("complete_task", {"task_id": "task-1"}, None),
        ("uncomplete_task", {"task_id": "task-1"}, None),
        ("delete_todoist_task", {"task_id": "task-1"}, None),
        (
            "get_completed_todoist_tasks_by_completion_date",
            {**EXPLICIT_COMPLETION_RANGE, "limit": 10},
            {"items": [{"id": "task-done"}], "next_cursor": "next"},
        ),
        (
            "get_comments",
            {"task_id": "task-1", "limit": 10, "comment_id": None},
            {"results": [{"id": "comment-1"}]},
        ),
        (
            "add_comment",
            {"content": "A note", "task_id": "task-1", "project_id": None},
            {"id": "comment-new"},
        ),
        (
            "get_labels",
            {"search": "work", "limit": 20},
            {
                "results": [{"name": "Work"}, {"name": "Home"}],
                "next_cursor": None,
            },
        ),
        (
            "get_projects",
            {"search": "mcp", "limit": 20},
            {
                "results": [{"name": "jarvis-mcp"}, {"name": "Inbox"}],
                "next_cursor": None,
            },
        ),
        (
            "create_project",
            {"name": "Reading", "color": None, "is_favorite": True},
            {"id": "project-new", "name": "Reading"},
        ),
    ],
)
def test_registered_async_handler_matches_sync_request_and_result(
    operation,
    arguments,
    provider_response,
):
    sync_client = TodoistApiClient(api_key="sync-token")
    async_client = TodoistApiClient(api_key="async-token")
    sync_request = Mock(return_value=copy.deepcopy(provider_response))
    async_request = AsyncMock(return_value=copy.deepcopy(provider_response))
    sync_client._request = sync_request
    async_client._request = Mock(side_effect=AssertionError("sync transport used"))
    async_client.async_request = async_request

    sync_spec = {spec.name: spec for spec in get_todoist_tool_specs(sync_client)}[
        operation
    ]
    async_spec = {spec.name: spec for spec in get_todoist_tool_specs(async_client)}[
        operation
    ]

    sync_result = sync_spec.handler(copy.deepcopy(arguments))
    async_result = asyncio.run(async_spec.async_handler(copy.deepcopy(arguments)))

    assert async_result == sync_result
    assert async_request.await_args.args == sync_request.call_args.args
    assert async_request.await_args.kwargs == sync_request.call_args.kwargs
    async_client._request.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        (
            "add_todoist_task",
            {"content": "bad", "duration": 3},
            "duration and duration_unit",
        ),
        (
            "update_todoist_task",
            {"task_id": "task-1", "labels": None},
            "labels must be an array",
        ),
        ("get_comments", {}, "requires task_id"),
        (
            "add_comment",
            {"content": "bad", "task_id": "1", "project_id": "2"},
            "exactly one",
        ),
        (
            "get_completed_todoist_tasks_by_completion_date",
            {
                "since": "2026-07-15T00:00:00Z",
                "until": "2026-07-01T00:00:00Z",
            },
            "later than since",
        ),
        ("get_tasks", {"limit": 0}, "between 1 and 200"),
        ("get_comments", {"task_id": "task-1", "limit": 11}, "between 1 and 10"),
    ],
)
def test_async_handler_preserves_sync_validation(operation, arguments, message):
    client = TodoistApiClient(api_key="token")
    client.async_request = AsyncMock()
    spec = {spec.name: spec for spec in get_todoist_tool_specs(client)}[operation]

    with pytest.raises(ValueError, match=message):
        asyncio.run(spec.async_handler(copy.deepcopy(arguments)))

    client.async_request.assert_not_awaited()


def test_registry_exposes_every_todoist_async_handler_without_changing_sync_map():
    client = TodoistApiClient(api_key="token")
    specs = get_todoist_tool_specs(client)
    registry = ToolRegistry().register(specs)
    schema_names = {
        schema["function"]["name"] for schema in get_todoist_tool_schemas()
    }

    assert set(registry.handler_map()) == schema_names
    assert set(registry.async_handler_map()) == schema_names
    assert all(inspect.iscoroutinefunction(spec.async_handler) for spec in specs)
    assert registry.mutating_names() == MUTATING_TOOL_NAMES


def test_async_handler_binding_preserves_immutable_tracer_and_transports():
    original = TodoistApiClient(api_key="token", tracer=NULL_TRACE)
    bound_tracer = Mock(spec=TracePrinter)
    bound = original.with_tracer(bound_tracer)
    specs = get_todoist_tool_specs(bound)

    assert original.tracer is NULL_TRACE
    assert bound.tracer is bound_tracer
    assert all(spec.handler.__self__ is bound for spec in specs)
    assert all(spec.async_handler.__self__ is bound for spec in specs)


def test_completed_async_handler_preserves_empty_result_shape():
    client = TodoistApiClient(api_key="token")
    client.async_request = AsyncMock(return_value=[])
    handler = {
        spec.name: spec.async_handler for spec in get_todoist_tool_specs(client)
    }["get_completed_todoist_tasks_by_completion_date"]

    result = asyncio.run(handler(dict(EXPLICIT_COMPLETION_RANGE)))

    assert result == {"items": [], "next_cursor": None}
