"""Resource restrictions are enforced before model and trace exposure."""

import asyncio

import httpx
import pytest

from agents.agent_api.app.tools.access_policy import (
    AccessDeniedError,
    ResourceAccessPolicy,
)
from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.dispatcher import ToolDispatcher
from agents.agent_api.app.tools.todoist.client import (
    TodoistApiClient,
    _todoist_trace_inputs,
    _todoist_trace_outputs,
)
from agents.agent_api.app.tracing import TracePrinter
from agents.agent_api.app.user_context.preferences import AccessPreferences


def _policy() -> ResourceAccessPolicy:
    return ResourceAccessPolicy(
        AccessPreferences.model_validate(
            {
                "restricted_todoist_projects": [
                    {"id": "private-project", "label": "Private"}
                ],
                "restricted_google_calendars": [
                    {
                        "id": "private-calendar",
                        "label": "Private calendar",
                        "is_primary": False,
                    }
                ],
            }
        )
    )


def test_targeted_restricted_resources_are_denied():
    policy = _policy()
    with pytest.raises(AccessDeniedError):
        policy.guard("get_tasks", {"project_id": "private-project"})
    with pytest.raises(AccessDeniedError):
        policy.guard(
            "list_calendar_events",
            {"calendar_id": "private-calendar"},
        )


def test_mixed_todoist_results_are_filtered_and_ids_stay_ungrounded():
    policy = _policy()
    result = policy.filter_result(
        "get_tasks",
        {
            "results": [
                {"id": "allowed-task", "project_id": "work", "content": "Visible"},
                {
                    "id": "private-task",
                    "project_id": "private-project",
                    "content": "Never expose",
                },
            ],
            "next_cursor": None,
        },
    )
    assert result["results"] == [
        {"id": "allowed-task", "project_id": "work", "content": "Visible"}
    ]
    assert "Never expose" not in str(result)
    with pytest.raises(AccessDeniedError):
        policy.guard("delete_todoist_task", {"task_id": "private-task"})

    comments = policy.filter_result(
        "get_comments",
        {
            "results": [
                {"id": "visible-comment", "task_id": "allowed-task"},
                {"id": "private-comment", "task_id": "private-task"},
            ]
        },
    )
    assert comments["results"] == [
        {"id": "visible-comment", "task_id": "allowed-task"}
    ]

    projects = policy.filter_result(
        "get_projects",
        {
            "results": [
                {"id": "work", "name": "Work"},
                {"id": "private-project", "name": "Never expose"},
            ]
        },
    )
    assert projects["results"] == [{"id": "work", "name": "Work"}]


def test_calendar_lists_and_freebusy_are_filtered():
    policy = _policy()
    calendars = policy.filter_result(
        "list_calendars",
        {
            "calendars": [
                {"calendar_id": "work", "summary": "Work"},
                {
                    "calendar_id": "private-calendar",
                    "summary": "Private calendar",
                },
            ]
        },
    )
    assert calendars["calendars"] == [{"calendar_id": "work", "summary": "Work"}]

    freebusy = policy.filter_result(
        "get_freebusy",
        {
            "calendars": {
                "work": {"busy": []},
                "private-calendar": {"busy": [{"start": "secret"}]},
            }
        },
    )
    assert freebusy == {"calendars": {"work": {"busy": []}}}


def test_provider_tracer_suppresses_payloads(capsys):
    policy = _policy()
    tracer = policy.provider_tracer(TracePrinter(enabled=True, show_payloads=True))
    tracer.event(
        "provider.request",
        "Safe metadata",
        status=200,
        error="private-project",
    )
    tracer.payload("provider.payload", "response", {"content": "Never expose"})
    output = capsys.readouterr().out
    assert "Safe metadata" in output
    assert "Never expose" not in output
    assert "private-project" not in output


def test_langsmith_processors_never_include_provider_content():
    inputs = _todoist_trace_inputs(
        {
            "url": "https://api.todoist.com/api/v1/tasks/1234567890123456",
            "method": "GET",
            "payload": {"content": "Never expose"},
        }
    )
    outputs = _todoist_trace_outputs(
        {"id": "1234567890123456", "content": "Never expose"}
    )

    assert "1234567890123456" not in str(inputs)
    assert "Never expose" not in str(inputs)
    assert "1234567890123456" not in str(outputs)
    assert "Never expose" not in str(outputs)


def test_dispatch_logs_only_filtered_results_and_sanitized_denials(capsys):
    policy = _policy()
    registry = ToolRegistry().register(
        [
            ToolSpec(
                name="get_tasks",
                openai_schema={"type": "function", "function": {"name": "get_tasks"}},
                handler=lambda _arguments: [
                    {"id": "visible", "project_id": "work", "content": "Visible"},
                    {
                        "id": "private-task",
                        "project_id": "private-project",
                        "content": "Never expose",
                    },
                ],
            )
        ]
    )
    dispatcher = ToolDispatcher(
        registry,
        tracer=TracePrinter(enabled=True, show_payloads=True),
        access_policy=policy,
    )

    result = dispatcher.execute_tool("call-read", "get_tasks", {})
    denied = dispatcher.execute_tool(
        "call-denied",
        "get_tasks",
        {"project_id": "private-project"},
    )

    output = capsys.readouterr().out
    assert "Visible" in str(result)
    assert "Never expose" not in str(result)
    assert "Never expose" not in output
    assert "private-project" not in output
    assert denied["classified_error"]["kind"] == "access_denied"
    assert denied["classified_error"]["retryable"] is False

    dispatcher.execute_tool(
        "call-preflight",
        "get_tasks",
        {"task_id": "unknown-private-task"},
    )
    output = capsys.readouterr().out
    assert "unknown-private-task" not in output


def test_cached_results_are_filtered_before_rebinding():
    policy = _policy()
    dispatcher = ToolDispatcher(ToolRegistry(), access_policy=policy)

    result = dispatcher._rebind_cached_result(
        {
            "success": True,
            "content": [
                {"id": "visible", "project_id": "work"},
                {"id": "private-task", "project_id": "private-project"},
            ],
        },
        "new-call",
        "get_tasks",
    )

    assert result["content"] == [{"id": "visible", "project_id": "work"}]
    assert result["idempotency_deduplicated"] is True


def test_restricted_task_is_preflighted_before_sync_mutation():
    policy = _policy()
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method != "GET":
            pytest.fail("restricted mutation reached Todoist")
        return httpx.Response(
            200,
            json={
                "id": "private-task",
                "project_id": "private-project",
                "content": "Never expose",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = TodoistApiClient(
            api_key="token",
            http_client=http_client,
            response_filter=policy.filter_todoist_provider_response,
        )
        with pytest.raises(AccessDeniedError):
            client.update_todoist_task(
                {"task_id": "private-task", "content": "Changed"}
            )

    assert methods == ["GET"]


def test_restricted_task_is_preflighted_before_async_mutation():
    policy = _policy()
    methods = []

    async def scenario():
        def handler(request):
            methods.append(request.method)
            if request.method != "GET":
                pytest.fail("restricted mutation reached Todoist")
            return httpx.Response(
                200,
                json={
                    "id": "private-task",
                    "project_id": "private-project",
                    "content": "Never expose",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = TodoistApiClient(
                api_key="token",
                async_http_client=http_client,
                response_filter=policy.filter_todoist_provider_response,
            )
            await client.async_update_todoist_task(
                {"task_id": "private-task", "content": "Changed"}
            )

    with pytest.raises(AccessDeniedError):
        asyncio.run(scenario())
    assert methods == ["GET"]
