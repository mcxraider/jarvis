import copy
import email.message
import io
import json
import sys
import tempfile
import threading
import urllib.error
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import jarvis
from agents.agent_api.app.graph import builder as builder_module
from agents.agent_api.app.tools.todoist import client as todoist_client_module
from agents.agent_api.app.graph.nodes import orchestrator as orchestrator_module


class FakeDeepSeekAgentClient:
    """Deterministic fake LLM for offline Jarvis graph tests."""

    def __init__(self, responses: List[Dict[str, Any]]):
        self.responses = [copy.deepcopy(response) for response in responses]
        self.calls: List[List[Dict[str, Any]]] = []
        # Records the tool schema list passed on each turn so tests can assert
        # which tools the selection layer exposed to the model.
        self.tool_calls: List[List[Dict[str, Any]]] = []

    def create_message(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        **kwargs,
    ) -> Dict[str, Any]:
        self.calls.append(copy.deepcopy(messages))
        self.tool_calls.append(copy.deepcopy(tools))
        if not self.responses:
            return {"role": "assistant", "content": "No fake response configured."}
        return copy.deepcopy(self.responses.pop(0))


class FakeTodoistClient:
    """Deterministic fake Todoist client for offline Jarvis graph tests."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def _record(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"tool_name": tool_name, "arguments": copy.deepcopy(arguments)})
        return {"fake": True, "tool_name": tool_name, "arguments": arguments}

    def add_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("add_todoist_task", arguments)

    def get_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("get_todoist_task", arguments)

    def get_tasks(self, arguments: Dict[str, Any]) -> Any:
        return {"results": [self._record("get_tasks", arguments)], "next_cursor": None}

    def get_tasks_by_filter(self, arguments: Dict[str, Any]) -> Any:
        return {
            "results": [self._record("get_tasks_by_filter", arguments)],
            "next_cursor": None,
        }

    def update_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("update_todoist_task", arguments)

    def complete_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("complete_task", arguments)

    def uncomplete_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("uncomplete_task", arguments)

    def delete_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        return self._record("delete_todoist_task", arguments)

    def get_completed_todoist_tasks_by_completion_date(self, arguments: Dict[str, Any]) -> Any:
        return [self._record("get_completed_todoist_tasks_by_completion_date", arguments)]

    def get_comments(self, arguments: Dict[str, Any]) -> Any:
        return self._record("get_comments", arguments)

    def add_comment(self, arguments: Dict[str, Any]) -> Any:
        return self._record("add_comment", arguments)

    def get_labels(self, arguments: Dict[str, Any]) -> Any:
        return self._record("get_labels", arguments)

    def get_projects(self, arguments: Dict[str, Any]) -> Any:
        return {"results": [self._record("get_projects", arguments)], "next_cursor": None}

    def create_project(self, arguments: Dict[str, Any]) -> Any:
        return self._record("create_project", arguments)


class SeededTodoistClient(FakeTodoistClient):
    """Fake client whose reads return tasks with real ids.

    The base FakeTodoistClient's reads omit ``id``, so the prior-read ID validation
    guard cannot verify any mutation against them. This subclass surfaces ids so
    read -> mutate flows pass validation.
    """

    def get_tasks(self, arguments: Dict[str, Any]) -> Any:
        self._record("get_tasks", arguments)
        return {"results": [{"id": "t1", "content": "Submit report"}], "next_cursor": None}

    def get_tasks_by_filter(self, arguments: Dict[str, Any]) -> Any:
        self._record("get_tasks_by_filter", arguments)
        return {"results": [{"id": "t1", "content": "Submit report"}], "next_cursor": None}

    def get_todoist_task(self, arguments: Dict[str, Any]) -> Any:
        self._record("get_todoist_task", arguments)
        return {"id": arguments.get("task_id"), "content": "Submit report"}


class ParallelTrackingTodoistClient(FakeTodoistClient):
    """Fake client that records overlapping tool execution."""

    def __init__(self, expected_concurrent_calls: int):
        super().__init__()
        self.active_calls = 0
        self.max_active_calls = 0
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(expected_concurrent_calls, timeout=2)

    def _record(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)

        try:
            self.barrier.wait()
            return super()._record(tool_name, arguments)
        finally:
            with self.lock:
                self.active_calls -= 1


class FailingTodoistClient(FakeTodoistClient):
    """Fake client that raises a structured Todoist API failure."""

    def get_tasks_by_filter(self, arguments: Dict[str, Any]) -> Any:
        del arguments
        raise jarvis.TodoistApiError(
            kind="deprecated",
            message="This Todoist endpoint is no longer available.",
            status_code=410,
            retryable=False,
            operation="todoist.request",
            url="https://api.todoist.com/api/v1/tasks/completed/by_completion_date",
            method="GET",
            attempts=1,
        )


class FakeUrlopenResponse:
    """Minimal context-manager response for urllib.request.urlopen tests."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def __enter__(self) -> "FakeUrlopenResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def http_error(
    status: int,
    body: str = "raw provider body",
    retry_after: str = "",
) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://api.todoist.com/api/v1/tasks",
        code=status,
        msg="error",
        hdrs=headers,
        fp=io.BytesIO(body.encode("utf-8")),
    )


def fake_tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class TodoistApiClientRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            todoist_max_retry_attempts=3,
            todoist_retry_total_timeout_seconds=8.0,
            todoist_retry_base_delay_seconds=0.1,
            todoist_retry_max_delay_seconds=1.0,
        )

    def request_client(self) -> jarvis.TodoistApiClient:
        return jarvis.TodoistApiClient(api_key="todoist-test-key", tracer=jarvis.NULL_TRACE)

    def test_rate_limit_retries_with_retry_after_then_succeeds(self) -> None:
        client = self.request_client()
        effects = [
            http_error(429, body="quota exceeded", retry_after="2"),
            FakeUrlopenResponse(200, b'{"id": "task-1"}'),
        ]

        with (
            patch.object(todoist_client_module, "settings", self.settings),
            patch.object(todoist_client_module.random, "uniform", return_value=0),
            patch.object(todoist_client_module.time, "sleep") as sleep,
            patch.object(
                todoist_client_module.urllib.request,
                "urlopen",
                side_effect=effects,
            ) as urlopen,
        ):
            result = client._request("https://api.todoist.com/api/v1/tasks")

        self.assertEqual(result, {"id": "task-1"})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_transient_server_error_retries_then_succeeds(self) -> None:
        client = self.request_client()
        effects = [
            http_error(503, body="service unavailable"),
            FakeUrlopenResponse(200, b'{"items": []}'),
        ]

        with (
            patch.object(todoist_client_module, "settings", self.settings),
            patch.object(todoist_client_module.random, "uniform", return_value=0),
            patch.object(todoist_client_module.time, "sleep") as sleep,
            patch.object(
                todoist_client_module.urllib.request,
                "urlopen",
                side_effect=effects,
            ) as urlopen,
        ):
            result = client._request("https://api.todoist.com/api/v1/tasks")

        self.assertEqual(result, {"items": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_retry_after_past_budget_does_not_retry(self) -> None:
        client = self.request_client()
        bounded_settings = SimpleNamespace(
            **{
                **self.settings.__dict__,
                "todoist_retry_total_timeout_seconds": 1.0,
            }
        )

        with (
            patch.object(todoist_client_module, "settings", bounded_settings),
            patch.object(
                todoist_client_module.urllib.request,
                "urlopen",
                side_effect=http_error(429, retry_after="5"),
            ) as urlopen,
        ):
            with self.assertRaises(jarvis.TodoistApiError) as raised:
                client._request("https://api.todoist.com/api/v1/tasks")

        self.assertEqual(raised.exception.kind, "rate-limit")
        self.assertEqual(raised.exception.retry_after_seconds, 5.0)
        self.assertEqual(urlopen.call_count, 1)

    def test_non_retryable_http_statuses_are_classified_without_retry(self) -> None:
        cases = {
            400: "validation",
            401: "auth",
            403: "auth",
            404: "not-found",
            410: "deprecated",
            422: "validation",
        }

        for status, kind in cases.items():
            with self.subTest(status=status):
                client = self.request_client()
                with (
                    patch.object(todoist_client_module, "settings", self.settings),
                    patch.object(
                        todoist_client_module.urllib.request,
                        "urlopen",
                        side_effect=http_error(status, "private details"),
                    ) as urlopen,
                ):
                    with self.assertRaises(jarvis.TodoistApiError) as raised:
                        client._request("https://api.todoist.com/api/v1/tasks")

                self.assertEqual(raised.exception.kind, kind)
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(urlopen.call_count, 1)
                self.assertNotIn("private details", str(raised.exception))

    def test_provider_validation_details_are_safely_exposed(self) -> None:
        body = json.dumps(
            {
                "error": "labels must be an array of labels",
                "error_code": 42,
                "error_tag": "BAD_REQUEST",
                "error_extra": {"event_id": "private-event-id"},
            }
        )
        client = self.request_client()
        with (
            patch.object(todoist_client_module, "settings", self.settings),
            patch.object(
                todoist_client_module.urllib.request,
                "urlopen",
                side_effect=http_error(400, body),
            ),
        ):
            with self.assertRaises(jarvis.TodoistApiError) as raised:
                client._request("https://api.todoist.com/api/v1/tasks/task-1", "POST", {})

        error = raised.exception
        self.assertEqual(error.provider_message, "labels must be an array of labels")
        self.assertEqual(error.provider_code, 42)
        self.assertEqual(error.provider_tag, "BAD_REQUEST")
        self.assertIn("labels must be an array of labels", str(error))
        self.assertNotIn("private-event-id", str(error.to_classifier_payload()))

    def test_missing_api_key_is_auth_without_network_call(self) -> None:
        client = jarvis.TodoistApiClient(api_key="", tracer=jarvis.NULL_TRACE)
        client.api_key = ""

        with patch.object(todoist_client_module.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(jarvis.TodoistApiError) as raised:
                client._request("https://api.todoist.com/api/v1/tasks")

        self.assertEqual(raised.exception.kind, "auth")
        self.assertFalse(raised.exception.retryable)
        urlopen.assert_not_called()

    def test_url_error_retries_then_raises_transient(self) -> None:
        client = self.request_client()
        effects = [
            urllib.error.URLError("temporary failure"),
            urllib.error.URLError("temporary failure"),
            urllib.error.URLError("temporary failure"),
        ]

        with (
            patch.object(todoist_client_module, "settings", self.settings),
            patch.object(todoist_client_module.random, "uniform", return_value=0),
            patch.object(todoist_client_module.time, "sleep"),
            patch.object(
                todoist_client_module.urllib.request,
                "urlopen",
                side_effect=effects,
            ) as urlopen,
        ):
            with self.assertRaises(jarvis.TodoistApiError) as raised:
                client._request("https://api.todoist.com/api/v1/tasks")

        self.assertEqual(raised.exception.kind, "transient")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.attempts, 3)
        self.assertEqual(urlopen.call_count, 3)

    def test_v1_task_list_and_filter_endpoints(self) -> None:
        client = self.request_client()
        page = {"results": [{"id": "task-1"}], "next_cursor": "next-page"}
        with patch.object(client, "_request", return_value=page) as request:
            self.assertEqual(
                client.get_tasks(
                    {
                        "parent_id": "parent-1",
                        "ids": ["task-1", "task-2"],
                        "cursor": "page-2",
                        "limit": 25,
                    }
                ),
                page,
            )
            request.assert_called_once_with(
                "https://api.todoist.com/api/v1/tasks?"
                "parent_id=parent-1&ids=task-1%2Ctask-2&cursor=page-2&limit=25"
            )

        with patch.object(client, "_request", return_value=page) as request:
            self.assertEqual(
                client.get_tasks_by_filter(
                    {"query": "Jun 25 & p1", "lang": "en", "limit": 10}
                ),
                page,
            )
            request.assert_called_once_with(
                "https://api.todoist.com/api/v1/tasks/filter?"
                "query=Jun+25+%26+p1&lang=en&limit=10"
            )

    def test_update_preserves_explicit_null_fields(self) -> None:
        client = self.request_client()
        with patch.object(client, "_request", return_value={"id": "task-1"}) as request:
            client.update_todoist_task(
                {
                    "task_id": "task-1",
                    "assignee_id": None,
                    "duration": None,
                    "duration_unit": None,
                    "deadline_date": None,
                }
            )
        request.assert_called_once_with(
            "https://api.todoist.com/api/v1/tasks/task-1",
            "POST",
            {
                "assignee_id": None,
                "duration": None,
                "duration_unit": None,
                "deadline_date": None,
            },
        )

    def test_update_priority_only_omits_none_fields(self) -> None:
        client = self.request_client()
        with patch.object(client, "_request", return_value={"id": "task-1"}) as request:
            client.update_todoist_task(
                {
                    "task_id": "task-1",
                    "priority": 4,
                    "content": None,
                }
            )
        request.assert_called_once_with(
            "https://api.todoist.com/api/v1/tasks/task-1",
            "POST",
            {"priority": 4},
        )

    def test_update_rejects_explicit_null_labels(self) -> None:
        client = self.request_client()
        with self.assertRaisesRegex(ValueError, "labels must be an array"):
            client.update_todoist_task({"task_id": "task-1", "labels": None})

    def test_completed_task_range_validation(self) -> None:
        client = self.request_client()
        with self.assertRaisesRegex(ValueError, "later than since"):
            client.get_completed_todoist_tasks_by_completion_date(
                {
                    "since": "2026-06-20T00:00:00Z",
                    "until": "2026-06-19T00:00:00Z",
                }
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed three months"):
            client.get_completed_todoist_tasks_by_completion_date(
                {
                    "since": "2026-01-01T00:00:00Z",
                    "until": "2026-05-01T00:00:00Z",
                }
            )
        with self.assertRaisesRegex(ValueError, "RFC3339"):
            client.get_completed_todoist_tasks_by_completion_date(
                {"since": "2026-01-01", "until": "2026-02-01T00:00:00Z"}
            )


class FakeOpenAIStatusError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"fake status error {status_code}")


class FakeOpenAIRateLimitError(FakeOpenAIStatusError):
    pass


class FakeOpenAITimeoutError(Exception):
    pass


class FakeOpenAIConnectionError(Exception):
    pass


class FakeOpenAICompletions:
    def __init__(self, effects: List[Any]):
        self.effects = list(effects)
        self.calls = 0

    def create(self, **_kwargs: Any) -> Any:
        self.calls += 1
        effect = self.effects.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        return SimpleNamespace(choices=[SimpleNamespace(message=copy.deepcopy(effect))])


class FakeOpenAIClient:
    def __init__(self, effects: List[Any]):
        self.completions = FakeOpenAICompletions(effects)
        self.chat = SimpleNamespace(completions=self.completions)


class ToolSelectionTests(unittest.TestCase):
    def _registry(self):
        from agents.agent_api.app.tools.registry_factory import (
            build_registry_from_clients,
        )

        return build_registry_from_clients(FakeTodoistClient())

    def test_static_selector_returns_full_catalogue(self) -> None:
        from agents.agent_api.app.tools.selection import StaticToolSelector

        registry = self._registry()
        selector = StaticToolSelector()

        for query in ("", "add a task tomorrow"):
            schemas = selector.select_schemas(query, registry)
            self.assertEqual(schemas, registry.openai_schemas())
            self.assertEqual(len(schemas), len(registry.specs))

    def test_default_selector_is_static_selector(self) -> None:
        from agents.agent_api.app.tools.selection import (
            DEFAULT_TOOL_SELECTOR,
            StaticToolSelector,
        )

        self.assertIsInstance(DEFAULT_TOOL_SELECTOR, StaticToolSelector)

    def test_run_jarvis_uses_default_selector_to_expose_all_tools(self) -> None:
        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}])
        registry = self._registry()

        jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        # The default (pass-through) selector exposes the entire catalogue.
        self.assertEqual(len(agent_client.tool_calls), 1)
        self.assertEqual(len(agent_client.tool_calls[0]), len(registry.specs))

    def test_agent_node_honours_injected_selector(self) -> None:
        recorded: List[str] = []

        class FirstToolOnlySelector:
            """Test selector: records the query and exposes only the first tool."""

            def select_schemas(self, query, registry):
                recorded.append(query)
                return registry.openai_schemas()[:1]

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}])

        jarvis.run_jarvis(
            user_prompt="narrow me",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            tool_selector=FirstToolOnlySelector(),
        )

        # The selector saw the user query and its narrowed list reached the model.
        self.assertEqual(recorded, ["narrow me"])
        self.assertEqual(len(agent_client.tool_calls[0]), 1)


class UsageAndRedactionTests(unittest.TestCase):
    def test_usage_from_response_reads_all_fields(self) -> None:
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=40,
                total_tokens=160,
                prompt_cache_hit_tokens=30,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
            )
        )

        usage = orchestrator_module.usage_from_response(response)

        self.assertEqual(usage.prompt_tokens, 120)
        self.assertEqual(usage.completion_tokens, 40)
        self.assertEqual(usage.total_tokens, 160)
        self.assertEqual(usage.cached_tokens, 30)
        self.assertEqual(usage.reasoning_tokens, 12)

    def test_usage_from_response_tolerates_missing_optional_fields(self) -> None:
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        )

        usage = orchestrator_module.usage_from_response(response)

        self.assertEqual(usage.total_tokens, 15)
        self.assertEqual(usage.cached_tokens, 0)
        self.assertEqual(usage.reasoning_tokens, 0)

    def test_usage_from_response_handles_missing_usage(self) -> None:
        usage = orchestrator_module.usage_from_response(SimpleNamespace())

        self.assertEqual(usage.total_tokens, 0)

    def test_usage_summary_aggregates_across_turns(self) -> None:
        total = orchestrator_module.UsageSummary()
        total.add(orchestrator_module.UsageSummary(prompt_tokens=10, total_tokens=12))
        total.add(orchestrator_module.UsageSummary(prompt_tokens=5, total_tokens=6, reasoning_tokens=2))

        self.assertEqual(total.prompt_tokens, 15)
        self.assertEqual(total.total_tokens, 18)
        self.assertEqual(total.reasoning_tokens, 2)

    def test_todoist_endpoint_template_strips_identifiers(self) -> None:
        base = todoist_client_module.TODOIST_REST_BASE_URL
        self.assertEqual(
            todoist_client_module.todoist_endpoint_template(f"{base}/tasks/9876543210/close"),
            "/tasks/{id}/close",
        )
        self.assertEqual(
            todoist_client_module.todoist_endpoint_template(f"{base}/tasks?filter=today"),
            "/tasks",
        )
        self.assertEqual(
            todoist_client_module.todoist_endpoint_template(f"{base}/tasks"),
            "/tasks",
        )


class JarvisGraphTests(unittest.TestCase):
    def test_system_prompt_uses_orchestrator_contract(self) -> None:
        prompt = jarvis.get_system_prompt()

        self.assertIn("You are Jarvis, Jerry's personal assistant agent", prompt)
        self.assertIn("connected services listed in Runtime context", prompt)
        self.assertIn("ask_user", prompt)
        self.assertIn("end after the completed action/result", prompt)
        self.assertNotIn("Maximum 20 loop iterations per user turn", prompt)

    def test_worker_prompt_available_for_worker_nodes(self) -> None:
        prompt = jarvis.get_worker_prompt()

        self.assertIn("spawned for exactly one subtask", prompt)
        self.assertIn("status: DONE | BLOCKED | FAILED", prompt)
        self.assertIn("Max 5 tool calls", prompt)

    def test_initial_user_message_includes_request_datetime(self) -> None:
        messages = jarvis.build_initial_messages("Show me today's tasks")

        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("Current request date and time:", messages[1]["content"])
        self.assertIn("User request:\nShow me today's tasks", messages[1]["content"])

    def run_graph_with_fakes(
        self,
        responses: List[Dict[str, Any]],
        allow_mutations: bool = False,
        max_agent_turns: int = jarvis.MAX_AGENT_TURNS,
    ) -> jarvis.JarvisState:
        return jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=allow_mutations,
            agent_client=FakeDeepSeekAgentClient(responses),
            todoist_client=FakeTodoistClient(),
            max_agent_turns=max_agent_turns,
            tracer=jarvis.NULL_TRACE,
        )

    def test_direct_final_response(self) -> None:
        result = self.run_graph_with_fakes([{"role": "assistant", "content": "Hello."}])

        self.assertEqual(result["final_response"], "Hello.")
        self.assertEqual(result["tool_results"], [])

    def test_question_like_text_is_a_final_response(self) -> None:
        responses = [
            "Want me to continue?",
            "Would you like me to continue",
            "Can you provide the project name",
        ]

        for response in responses:
            with self.subTest(response=response):
                result = self.run_graph_with_fakes(
                    [{"role": "assistant", "content": response}]
                )

                self.assertEqual(result["final_response"], response)
                self.assertEqual(result["next"], "end")
                self.assertNotIn("__interrupt__", result)
                self.assertFalse(result.get("interrupted", False))

    def test_run_jarvis_generates_request_id_when_omitted(self) -> None:
        captured: List[Dict[str, Any]] = []

        # Capture the config passed into app.invoke by wrapping create_jarvis_graph.
        real_create = builder_module.create_jarvis_graph

        def capturing_create(*args: Any, **kwargs: Any):
            app = real_create(*args, **kwargs)
            real_app_invoke = app.invoke

            def invoke_spy(state: Any, config: Dict[str, Any]):
                captured.append(config)
                return real_app_invoke(state, config)

            app.invoke = invoke_spy  # type: ignore[assignment]
            return app

        with patch.object(builder_module, "create_jarvis_graph", side_effect=capturing_create):
            result = jarvis.run_jarvis(
                user_prompt="fake prompt",
                agent_client=FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}]),
                todoist_client=FakeTodoistClient(),
                tracer=jarvis.NULL_TRACE,
            )

        self.assertEqual(result["final_response"], "Hi.")
        self.assertEqual(len(captured), 1)
        config = captured[0]
        self.assertEqual(config["run_name"], "jarvis.invoke")
        self.assertEqual(config["metadata"]["invocation_type"], "invoke")
        self.assertTrue(config["metadata"]["request_id"])
        self.assertEqual(config["metadata"]["thread_id"], result["thread_id"])
        self.assertIn("invoke", config["tags"])

    def test_run_jarvis_preserves_supplied_request_id_and_thread(self) -> None:
        captured: List[Dict[str, Any]] = []
        real_create = builder_module.create_jarvis_graph

        def capturing_create(*args: Any, **kwargs: Any):
            app = real_create(*args, **kwargs)
            real_app_invoke = app.invoke

            def invoke_spy(state: Any, config: Dict[str, Any]):
                captured.append(config)
                return real_app_invoke(state, config)

            app.invoke = invoke_spy  # type: ignore[assignment]
            return app

        with patch.object(builder_module, "create_jarvis_graph", side_effect=capturing_create):
            jarvis.run_jarvis(
                user_prompt="fake prompt",
                thread_id="thread-xyz",
                request_id="tg_corr",
                agent_client=FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}]),
                todoist_client=FakeTodoistClient(),
                tracer=jarvis.NULL_TRACE,
            )

        metadata = captured[0]["metadata"]
        self.assertEqual(metadata["request_id"], "tg_corr")
        self.assertEqual(metadata["thread_id"], "thread-xyz")

    def test_request_source_is_kept_in_state_and_interrupt_payload(self) -> None:
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            request_source="test",
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call(
                                "call_ask",
                                jarvis.ASK_USER_TOOL_NAME,
                                {"question": "Which task should I update?"},
                            )
                        ],
                    }
                ]
            ),
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["request_source"], "test")
        self.assertEqual(result["interrupt_payload"]["request_source"], "test")

    def test_run_jarvis_sequence_invokes_prompts_in_order(self) -> None:
        agent_client = FakeDeepSeekAgentClient(
            [
                {"role": "assistant", "content": "First done."},
                {"role": "assistant", "content": "Second done."},
            ]
        )

        results = jarvis.run_jarvis_sequence(
            ["first prompt", "second prompt"],
            allow_mutations=False,
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(
            [result["final_response"] for result in results],
            ["First done.", "Second done."],
        )
        self.assertIn("User request:\nfirst prompt", agent_client.calls[0][1]["content"])
        self.assertIn("User request:\nsecond prompt", agent_client.calls[1][1]["content"])

    def test_load_user_prompts_from_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.txt"
            path.write_text(
                "\n".join(["# ignored comment", "first prompt", "", "second prompt"]),
                encoding="utf-8",
            )

            prompts = jarvis.load_user_prompts_from_file(str(path))

        self.assertEqual(prompts, ["first prompt", "second prompt"])

    def test_load_user_prompts_from_json_object_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.json"
            path.write_text(
                json.dumps({"prompts": ["first prompt", "second prompt"]}),
                encoding="utf-8",
            )

            prompts = jarvis.load_user_prompts_from_file(str(path))

        self.assertEqual(prompts, ["first prompt", "second prompt"])

    def test_collect_cli_prompts_combines_files_flags_and_positionals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.txt"
            path.write_text("file prompt\n", encoding="utf-8")
            args = jarvis.build_arg_parser().parse_args(
                [
                    "--prompts-file",
                    str(path),
                    "--prompt",
                    "flag prompt",
                    "--source",
                    "test",
                    "positional prompt",
                ]
            )

            prompts = jarvis.collect_cli_prompts(args)

        self.assertEqual(prompts, ["file prompt", "flag prompt", "positional prompt"])
        self.assertTrue(args.allow_mutations)
        self.assertEqual(args.source, "test")

    def test_json_summary_includes_run_log_path_when_available(self) -> None:
        summary = jarvis.result_to_json_summary(
            {
                "thread_id": "thread-1",
                "final_response": "Done.",
                "run_log_path": "/tmp/jarvis-run.log",
            },
            "hello",
            1,
        )

        self.assertEqual(summary["run_log_path"], "/tmp/jarvis-run.log")

    def test_main_prints_each_run_log_path(self) -> None:
        results = [
            {"final_response": "First.", "run_log_path": "/tmp/first.log"},
            {"final_response": "Second.", "run_log_path": "/tmp/second.log"},
        ]
        with patch("agents.agent_api.app.runner.run_jarvis_sequence", return_value=results), \
            patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = jarvis.main(["first", "second"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Run log: /tmp/first.log", stdout.getvalue())
        self.assertIn("Run log: /tmp/second.log", stdout.getvalue())

    def test_main_json_output_contains_log_path_and_remains_valid_json(self) -> None:
        results = [{"final_response": "Done.", "run_log_path": "/tmp/run.log"}]
        with patch("agents.agent_api.app.runner.run_jarvis_sequence", return_value=results), \
            patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = jarvis.main(["--json", "hello"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["results"][0]["run_log_path"], "/tmp/run.log")

    def test_local_clarification_wrapper_prompts_and_resumes(self) -> None:
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Which task should I update?"},
                        )
                    ],
                },
                {"role": "assistant", "content": "Updated the dentist task."},
            ]
        )

        with patch(
            "agents.agent_api.app.runner.ask_user_for_clarification",
            return_value="the dentist task",
        ) as ask:
            result = jarvis.run_jarvis_with_local_clarifications(
                user_prompt="update my task",
                allow_mutations=False,
                agent_client=agent_client,
                todoist_client=FakeTodoistClient(),
                tracer=jarvis.NULL_TRACE,
            )

        self.assertFalse(result["interrupted"])
        self.assertEqual(result["final_response"], "Updated the dentist task.")
        ask.assert_called_once()
        self.assertEqual(ask.call_args[0][0]["question"], "Which task should I update?")

    def test_ask_user_tool_schema_available(self) -> None:
        tools = jarvis.get_todoist_tools()
        ask_user_tools = [
            tool
            for tool in tools
            if tool.get("function", {}).get("name") == jarvis.ASK_USER_TOOL_NAME
        ]

        self.assertEqual(len(ask_user_tools), 1)
        parameters = ask_user_tools[0]["function"]["parameters"]
        self.assertEqual(parameters["required"], ["question"])
        self.assertIn("reason", parameters["properties"])
        self.assertIn("missing_fields", parameters["properties"])
        self.assertIn("risk", parameters["properties"])

    def test_todoist_v1_list_tool_schemas_are_separate(self) -> None:
        tools_by_name = {
            tool["function"]["name"]: tool["function"] for tool in jarvis.get_todoist_tools()
        }
        list_parameters = tools_by_name["get_tasks"]["parameters"]
        filter_parameters = tools_by_name["get_tasks_by_filter"]["parameters"]

        self.assertNotIn("filter", list_parameters["properties"])
        self.assertNotIn("lang", list_parameters["properties"])
        self.assertIn("parent_id", list_parameters["properties"])
        self.assertIn("cursor", list_parameters["properties"])
        self.assertEqual(filter_parameters["required"], ["query"])

    def test_single_read_tool_call(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks_by_filter", {"query": "today"})],
                },
                {"role": "assistant", "content": "You have one task today."},
            ]
        )

        self.assertEqual(result["final_response"], "You have one task today.")
        self.assertEqual(len(result["tool_results"]), 1)
        self.assertTrue(result["tool_results"][0]["success"])

    def test_ask_user_tool_call_interrupts(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {
                                "question": "Which task should I update?",
                                "reason": "Multiple tasks match.",
                                "missing_fields": ["task_id"],
                                "risk": "I might update the wrong task.",
                            },
                        )
                    ],
                }
            ]
        )

        self.assertTrue(result["interrupted"])
        self.assertIn("__interrupt__", result)
        self.assertEqual(result["next"], "hitl")
        self.assertEqual(result["interrupt_payload"]["question"], "Which task should I update?")
        self.assertEqual(result["interrupt_payload"]["tool_call_id"], "call_ask")
        self.assertEqual(result["final_response"], "")

    def test_resume_appends_hitl_tool_message_and_user_reply(self) -> None:
        thread_id = "test-hitl-resume"
        checkpointer = jarvis.InMemorySaver()
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Which task should I update?"},
                        )
                    ],
                },
                {"role": "assistant", "content": "Updated the selected task."},
            ]
        )

        interrupted = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )
        self.assertTrue(interrupted["interrupted"])

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            clarification_reply="the dentist task",
            checkpointer=checkpointer,
        )

        self.assertFalse(result["interrupted"])
        self.assertEqual(result["final_response"], "Updated the selected task.")
        roles = [message.get("role") for message in result["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user", "assistant"])
        self.assertEqual(result["messages"][3]["tool_call_id"], "call_ask")
        self.assertEqual(result["messages"][3]["name"], jarvis.ASK_USER_TOOL_NAME)
        self.assertEqual(json.loads(result["messages"][3]["content"])["user_reply"], "the dentist task")
        self.assertIn("Clarification received", result["messages"][4]["content"])
        self.assertEqual(len(agent_client.calls), 2)
        second_call_roles = [message.get("role") for message in agent_client.calls[1]]
        self.assertEqual(second_call_roles, ["system", "user", "assistant", "tool", "user"])

    def test_mutating_tool_blocked(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call("call_1", "add_todoist_task", {"content": "Buy milk"})
                    ],
                },
                {
                    "role": "assistant",
                    "content": "I did not create the task because mutations are blocked.",
                },
            ],
            allow_mutations=False,
        )

        tool_result = result["tool_results"][0]
        self.assertFalse(tool_result["success"])
        self.assertTrue(tool_result["mutation_blocked"])

    def test_mutating_tool_executed_when_allowed(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call("call_1", "add_todoist_task", {"content": "Buy milk"})
                    ],
                },
                {"role": "assistant", "content": "Created the task."},
            ],
            allow_mutations=True,
        )

        tool_result = result["tool_results"][0]
        self.assertTrue(tool_result["success"])
        self.assertFalse(tool_result["mutation_blocked"])

    def test_update_tool_preserves_explicit_nulls_and_omits_missing_fields(self) -> None:
        # A read first surfaces task-1 so prior-read ID validation lets the update run.
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_read", "get_todoist_task", {"task_id": "task-1"})
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call(
                                "call_1",
                                "update_todoist_task",
                                {
                                    "task_id": "task-1",
                                    "assignee_id": None,
                                    "duration": None,
                                    "duration_unit": None,
                                    "deadline_date": None,
                                },
                            )
                        ],
                    },
                    {"role": "assistant", "content": "Cleared the scheduling fields."},
                ]
            ),
            todoist_client=SeededTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        arguments = result["tool_results"][-1]["content"]["arguments"]
        self.assertEqual(
            arguments,
            {
                "task_id": "task-1",
                "assignee_id": None,
                "duration": None,
                "duration_unit": None,
                "deadline_date": None,
            },
        )

    def test_update_tool_priority_only_omits_missing_fields(self) -> None:
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_read", "get_todoist_task", {"task_id": "task-1"})
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call(
                                "call_update",
                                "update_todoist_task",
                                {"task_id": "task-1", "priority": 4},
                            )
                        ],
                    },
                    {"role": "assistant", "content": "Raised the priority."},
                ]
            ),
            todoist_client=SeededTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        arguments = result["tool_results"][-1]["content"]["arguments"]
        self.assertEqual(arguments, {"task_id": "task-1", "priority": 4})

    def test_prior_read_lets_mutation_on_seen_id_execute(self) -> None:
        # Happy path: read surfaces t1, then completing t1 executes normally.
        todoist_client = SeededTodoistClient()
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_read", "get_tasks_by_filter", {"query": "today"})
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [fake_tool_call("call_done", "complete_task", {"task_id": "t1"})],
                    },
                    {"role": "assistant", "content": "Done."},
                ]
            ),
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["final_response"], "Done.")
        executed = [call["tool_name"] for call in todoist_client.calls]
        self.assertIn("complete_task", executed)
        self.assertTrue(result["tool_results"][-1]["success"])

    def test_hallucinated_id_is_blocked_before_execution(self) -> None:
        # complete_task targets an id no read ever returned -> never reaches the client.
        todoist_client = FakeTodoistClient()
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_bad", "complete_task", {"task_id": "ghost"})],
                },
                {"role": "assistant", "content": "Let me look that up first."},
            ]
        )

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=agent_client,
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
        )

        # No Todoist mutation happened.
        self.assertEqual([c["tool_name"] for c in todoist_client.calls], [])
        # A synthetic "unverified" tool result was fed back to the model.
        blocked = [
            json.loads(message["content"])
            for message in result["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_bad"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(blocked[0]["unverified_entity"])
        self.assertFalse(blocked[0]["success"])
        # The agent got another turn to recover.
        self.assertEqual(len(agent_client.calls), 2)
        self.assertEqual(result["final_response"], "Let me look that up first.")

    def test_same_turn_read_and_mutation_blocks_the_mutation(self) -> None:
        # Read + complete emitted together: the read hasn't executed yet, so t1 is
        # unseen and the whole batch is deferred — nothing reaches the client.
        todoist_client = SeededTodoistClient()
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_read", "get_tasks_by_filter", {"query": "today"}),
                            fake_tool_call("call_mut", "complete_task", {"task_id": "t1"}),
                        ],
                    },
                    {"role": "assistant", "content": "I'll read first next time."},
                ]
            ),
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
        )

        self.assertNotIn("complete_task", [c["tool_name"] for c in todoist_client.calls])
        mutation_result = [
            json.loads(message["content"])
            for message in result["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_mut"
        ]
        self.assertEqual(len(mutation_result), 1)
        self.assertTrue(mutation_result[0]["unverified_entity"])

    def test_agent_recovers_by_reading_then_mutating_across_turns(self) -> None:
        # Same-turn batch is blocked; the agent then reads alone and completes on the
        # next turn, which now passes validation and executes exactly once.
        todoist_client = SeededTodoistClient()
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_read1", "get_tasks_by_filter", {"query": "today"}),
                            fake_tool_call("call_mut1", "complete_task", {"task_id": "t1"}),
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_read2", "get_tasks_by_filter", {"query": "today"})
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [fake_tool_call("call_mut2", "complete_task", {"task_id": "t1"})],
                    },
                    {"role": "assistant", "content": "Completed."},
                ]
            ),
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["final_response"], "Completed.")
        completes = [c for c in todoist_client.calls if c["tool_name"] == "complete_task"]
        self.assertEqual(len(completes), 1)  # only the post-read completion executed

    def test_mixed_ask_user_and_mutating_tool_defers_mutation(self) -> None:
        thread_id = "test-hitl-mixed"
        checkpointer = jarvis.InMemorySaver()
        todoist_client = FakeTodoistClient()
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Should I create this task?"},
                        ),
                        fake_tool_call("call_add", "add_todoist_task", {"content": "Buy milk"}),
                    ],
                },
                {"role": "assistant", "content": "I will wait for a clean tool plan."},
            ]
        )

        interrupted = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=agent_client,
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )

        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(interrupted["interrupt_payload"]["deferred_tool_calls"][0]["id"], "call_add")
        self.assertEqual(todoist_client.calls, [])

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            allow_mutations=True,
            agent_client=agent_client,
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            clarification_reply="yes",
            checkpointer=checkpointer,
        )

        self.assertEqual(todoist_client.calls, [])
        deferred_messages = [
            message
            for message in result["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "call_add"
        ]
        self.assertEqual(len(deferred_messages), 1)
        self.assertTrue(json.loads(deferred_messages[0]["content"])["deferred_for_clarification"])

    def test_multiple_tool_loop_turns(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks_by_filter", {"query": "today"})],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        fake_tool_call(
                            "call_2",
                            "get_completed_todoist_tasks_by_completion_date",
                            {"limit": 5},
                        )
                    ],
                },
                {"role": "assistant", "content": "Here is the combined summary."},
            ]
        )

        self.assertEqual(len(result["tool_results"]), 2)
        self.assertEqual(result["final_response"], "Here is the combined summary.")

    def test_multiple_tool_calls_in_one_turn_execute_in_parallel(self) -> None:
        todoist_client = ParallelTrackingTodoistClient(expected_concurrent_calls=2)
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            fake_tool_call("call_today", "get_tasks_by_filter", {"query": "today"}),
                            fake_tool_call("call_tomorrow", "get_tasks_by_filter", {"query": "tomorrow"}),
                        ],
                    },
                    {"role": "assistant", "content": "Here are both lists."},
                ]
            ),
            todoist_client=todoist_client,
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["final_response"], "Here are both lists.")
        self.assertEqual(
            [item["tool_call_id"] for item in result["tool_results"]],
            ["call_today", "call_tomorrow"],
        )
        self.assertEqual(len(todoist_client.calls), 2)
        self.assertGreaterEqual(todoist_client.max_active_calls, 2)

    def test_unsupported_tool_returns_error(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "unknown_tool", {})],
                },
                {"role": "assistant", "content": "That tool is unsupported."},
            ]
        )

        tool_result = result["tool_results"][0]
        self.assertFalse(tool_result["success"])
        self.assertIn("Unsupported tool", tool_result["error"])

    def test_classified_todoist_error_survives_tool_result_and_message(self) -> None:
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=FakeDeepSeekAgentClient(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [fake_tool_call("call_1", "get_tasks_by_filter", {"query": "today"})],
                    },
                    {"role": "assistant", "content": "Todoist is unavailable."},
                ]
            ),
            todoist_client=FailingTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        tool_result = result["tool_results"][0]
        self.assertFalse(tool_result["success"])
        self.assertEqual(tool_result["error"], "This Todoist endpoint is no longer available.")
        self.assertEqual(tool_result["classified_error"]["source"], "todoist")
        self.assertEqual(tool_result["classified_error"]["kind"], "deprecated")
        self.assertEqual(tool_result["classified_error"]["status_code"], 410)
        self.assertFalse(tool_result["classified_error"]["retryable"])

        tool_messages = [message for message in result["messages"] if message.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        message_content = json.loads(tool_messages[0]["content"])
        self.assertEqual(message_content["classified_error"]["kind"], "deprecated")
        self.assertNotIn("api/v1/tasks/completed", tool_messages[0]["content"])

    def test_max_turn_guard(self) -> None:
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks_by_filter", {"query": "today"})],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [fake_tool_call("call_2", "get_tasks_by_filter", {"query": "tomorrow"})],
                },
            ]
        )
        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            max_agent_turns=1,
        )

        self.assertIn("Max agent turns exceeded", result["error"])
        self.assertEqual(
            result["final_response"],
            "Max number of turns reached for this agent. Simplify your query.",
        )
        self.assertEqual(result["next"], "end")
        self.assertEqual(len(agent_client.calls), 1)

    def test_reasoning_content_preserved(self) -> None:
        result = self.run_graph_with_fakes(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private chain metadata",
                    "tool_calls": [fake_tool_call("call_1", "get_tasks_by_filter", {"query": "today"})],
                },
                {"role": "assistant", "content": "Done."},
            ]
        )

        assistant_messages = [
            message for message in result["messages"] if message.get("role") == "assistant"
        ]
        self.assertEqual(
            assistant_messages[0].get("reasoning_content"),
            "private chain metadata",
        )

    def test_reasoning_content_preserved_across_hitl_resume(self) -> None:
        thread_id = "test-hitl-reasoning"
        checkpointer = jarvis.InMemorySaver()
        agent_client = FakeDeepSeekAgentClient(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "private ask metadata",
                    "tool_calls": [
                        fake_tool_call(
                            "call_ask",
                            jarvis.ASK_USER_TOOL_NAME,
                            {"question": "Which one?"},
                        )
                    ],
                },
                {"role": "assistant", "content": "Done."},
            ]
        )
        jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            checkpointer=checkpointer,
        )

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
            thread_id=thread_id,
            clarification_reply="this one",
            checkpointer=checkpointer,
        )

        assistant_messages = [
            message for message in result["messages"] if message.get("role") == "assistant"
        ]
        self.assertEqual(assistant_messages[0].get("reasoning_content"), "private ask metadata")
        self.assertEqual(
            agent_client.calls[1][2].get("reasoning_content"),
            "private ask metadata",
        )

    def test_deepseek_client_retries_retryable_failures(self) -> None:
        cases = [
            (FakeOpenAIRateLimitError(429), "rate_limit"),
            (FakeOpenAIStatusError(503), "server_error"),
            (FakeOpenAITimeoutError("timeout"), "timeout"),
            (FakeOpenAIConnectionError("connection"), "connection_error"),
        ]

        for error, expected_type in cases:
            with self.subTest(expected_type=expected_type):
                client = jarvis.DeepSeekAgentClient(
                    api_key="test",
                    tracer=jarvis.NULL_TRACE,
                    retry_sleep=lambda _seconds: None,
                )
                fake_client = FakeOpenAIClient(
                    [error, {"role": "assistant", "content": "Recovered."}]
                )
                client.client = fake_client

                with patch.multiple(
                    orchestrator_module,
                    APIStatusError=FakeOpenAIStatusError,
                    RateLimitError=FakeOpenAIRateLimitError,
                    APITimeoutError=FakeOpenAITimeoutError,
                    APIConnectionError=FakeOpenAIConnectionError,
                ):
                    message = client.create_message(
                        [{"role": "user", "content": "hello"}],
                        [],
                    )
                    self.assertEqual(client._error_type(error), expected_type)

                self.assertEqual(message["content"], "Recovered.")
                self.assertEqual(fake_client.completions.calls, 2)

    def test_deepseek_client_does_not_retry_client_status_errors(self) -> None:
        cases = [400, 401, 422]

        for status_code in cases:
            with self.subTest(status_code=status_code):
                client = jarvis.DeepSeekAgentClient(
                    api_key="test",
                    tracer=jarvis.NULL_TRACE,
                    retry_sleep=lambda _seconds: None,
                )
                fake_client = FakeOpenAIClient([FakeOpenAIStatusError(status_code)])
                client.client = fake_client

                with patch.multiple(
                    orchestrator_module,
                    APIStatusError=FakeOpenAIStatusError,
                    RateLimitError=FakeOpenAIRateLimitError,
                    APITimeoutError=FakeOpenAITimeoutError,
                    APIConnectionError=FakeOpenAIConnectionError,
                ):
                    with self.assertRaises(jarvis.DeepSeekAgentClientError) as raised:
                        client.create_message([{"role": "user", "content": "hello"}], [])

                self.assertEqual(fake_client.completions.calls, 1)
                self.assertEqual(raised.exception.payload["type"], "client_error")
                self.assertFalse(raised.exception.payload["retryable"])
                self.assertEqual(raised.exception.payload["status_code"], status_code)

    def test_deepseek_final_failure_ends_graph_with_structured_error(self) -> None:
        payload = {
            "source": "deepseek",
            "type": "timeout",
            "retryable": True,
            "attempts": 3,
            "message": "timed out",
        }

        class FailingDeepSeekAgentClient:
            def create_message(
                self,
                _messages: List[Dict[str, Any]],
                _tools: List[Dict[str, Any]],
                **kwargs,
            ) -> Dict[str, Any]:
                raise jarvis.DeepSeekAgentClientError(payload)

        result = jarvis.run_jarvis(
            user_prompt="fake prompt",
            agent_client=FailingDeepSeekAgentClient(),
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        self.assertEqual(result["next"], "end")
        self.assertEqual(result["final_response"], jarvis.LLM_FAILURE_MESSAGE)
        self.assertEqual(json.loads(result["error"]), payload)


class ContextGateTests(unittest.TestCase):
    """The runtime user gate must reject before any model invocation (#4).

    When a Telegram id is present and a DSN is configured, ``run_jarvis`` resolves the
    canonical user first. An unknown/disabled user makes ``resolve_runtime_context``
    raise, and that must abort the run before the agent client is ever called.
    """

    def test_rejected_user_never_reaches_the_model(self) -> None:
        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "hi"}])

        # ``settings`` is a frozen dataclass; the gate raises before any other
        # settings field is read, so a minimal stand-in with a truthy DSN suffices.
        with patch.object(
            builder_module, "settings", SimpleNamespace(postgres_dsn="postgresql://fake")
        ), patch.object(
            builder_module,
            "resolve_runtime_context",
            side_effect=PermissionError("No active Jarvis user for this identity."),
        ):
            with self.assertRaises(PermissionError):
                jarvis.run_jarvis(
                    user_prompt="do something",
                    agent_client=agent_client,
                    todoist_client=FakeTodoistClient(),
                    tracer=jarvis.NULL_TRACE,
                    telegram_user_id=999,
                )

        # The gate fired before the model: the fake LLM recorded no calls.
        self.assertEqual(agent_client.calls, [])
        self.assertEqual(agent_client.tool_calls, [])

    def test_resolved_user_reaches_the_model(self) -> None:
        # Non-vacuity companion: on the normal (ungated) path the client IS invoked.
        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "hi"}])

        jarvis.run_jarvis(
            user_prompt="do something",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=jarvis.NULL_TRACE,
        )

        self.assertTrue(agent_client.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
