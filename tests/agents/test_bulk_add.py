"""Tests for bulk_add_todoist_tasks — client method, risk classification, and dispatch."""

import json
import time
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from agents.agent_api.app.graph.risk import classify_risk
from agents.agent_api.app.tools.todoist.client import TodoistApiClient, TodoistApiError
from agents.agent_api.app.tools.todoist.schemas import MUTATING_TOOL_NAMES, get_todoist_tool_schemas
from agents.agent_api.app.tools.todoist.tools import TodoistToolDispatcher, get_todoist_tool_specs


class TestBulkAddClient:
    def _make_client(self):
        client = TodoistApiClient(api_key="test-key")
        return client

    @patch.object(TodoistApiClient, "_request")
    def test_creates_n_tasks(self, mock_request):
        """Calls _request N times for count=N."""
        mock_request.return_value = {"id": "123", "content": "test"}
        client = self._make_client()

        result = client.bulk_add_todoist_tasks({"content": "test task", "count": 5})

        assert mock_request.call_count == 5
        assert result["created_count"] == 5
        assert result["requested_count"] == 5
        assert len(result["tasks"]) == 5
        assert result["errors"] is None

    @patch.object(TodoistApiClient, "_request")
    def test_single_count_skips_threadpool(self, mock_request):
        """count=1 calls _request directly without ThreadPoolExecutor."""
        mock_request.return_value = {"id": "abc", "content": "solo"}
        client = self._make_client()

        result = client.bulk_add_todoist_tasks({"content": "solo", "count": 1})

        mock_request.assert_called_once()
        assert result["created_count"] == 1
        assert result["tasks"] == [{"id": "abc", "content": "solo"}]

    @patch.object(TodoistApiClient, "_request")
    def test_partial_failure(self, mock_request):
        """One failed request doesn't kill others; errors are reported."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 3:
                raise RuntimeError("Network error")
            return {"id": f"task_{call_count[0]}", "content": "test"}

        mock_request.side_effect = side_effect
        client = self._make_client()

        result = client.bulk_add_todoist_tasks({"content": "test", "count": 5})

        assert result["requested_count"] == 5
        assert result["created_count"] == 4
        assert len(result["errors"]) == 1
        assert "Network error" in result["errors"][0]["error"]

    @patch.object(TodoistApiClient, "_request")
    def test_filters_none_values(self, mock_request):
        """None fields are stripped before sending to Todoist."""
        mock_request.return_value = {"id": "1", "content": "x"}
        client = self._make_client()

        client.bulk_add_todoist_tasks({
            "content": "test",
            "count": 2,
            "description": None,
            "project_id": "proj_1",
            "labels": None,
            "priority": 4,
        })

        # Check the payload sent to _request has no None values
        for call in mock_request.call_args_list:
            payload = call[0][2]  # 3rd positional arg is payload
            assert None not in payload.values()
            assert "content" in payload
            assert "project_id" in payload
            assert "priority" in payload
            assert "description" not in payload
            assert "labels" not in payload

    @patch.object(TodoistApiClient, "_request")
    def test_passes_all_fields(self, mock_request):
        """All non-None fields are forwarded to _request."""
        mock_request.return_value = {"id": "1", "content": "x"}
        client = self._make_client()

        client.bulk_add_todoist_tasks({
            "content": "meeting prep",
            "count": 3,
            "due_string": "tomorrow",
            "priority": 3,
            "labels": ["work"],
            "project_id": "p1",
            "section_id": "s1",
        })

        payload = mock_request.call_args_list[0][0][2]
        assert payload["content"] == "meeting prep"
        assert payload["due_string"] == "tomorrow"
        assert payload["priority"] == 3
        assert payload["labels"] == ["work"]
        assert payload["project_id"] == "p1"
        assert payload["section_id"] == "s1"
        assert "count" not in payload


class TestBulkAddRiskClassification:
    def test_always_risky_with_zero_mutations(self):
        """bulk_add is risky regardless of mutation count."""
        tool_call = {"id": "call_1", "function": {"name": "bulk_add_todoist_tasks", "arguments": "{}"}}
        state = {"tool_results": []}
        assert classify_risk(tool_call, state) == "risky"

    def test_always_risky_with_existing_mutations(self):
        """Still risky even with no prior mutations this turn."""
        tool_call = {"id": "call_1", "function": {"name": "bulk_add_todoist_tasks", "arguments": "{}"}}
        state = {"tool_results": [{"tool_name": "add_todoist_task"}] * 3}
        assert classify_risk(tool_call, state) == "risky"


class TestBulkAddSchema:
    def test_schema_registered(self):
        """bulk_add_todoist_tasks appears in the schema list."""
        schemas = get_todoist_tool_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "bulk_add_todoist_tasks" in names

    def test_schema_has_required_fields(self):
        """Schema requires content and count."""
        schemas = get_todoist_tool_schemas()
        bulk_schema = next(s for s in schemas if s["function"]["name"] == "bulk_add_todoist_tasks")
        params = bulk_schema["function"]["parameters"]
        assert "content" in params["required"]
        assert "count" in params["required"]
        assert params["properties"]["count"]["minimum"] == 2
        assert params["properties"]["count"]["maximum"] == 50

    def test_in_mutating_tool_names(self):
        """bulk_add is registered as a mutating tool."""
        assert "bulk_add_todoist_tasks" in MUTATING_TOOL_NAMES


class TestBulkAddDispatch:
    def test_dispatches_through_registry(self):
        """Full dispatch path: registry → handler → result envelope."""
        mock_client = MagicMock()
        mock_client.bulk_add_todoist_tasks.return_value = {
            "created_count": 3,
            "requested_count": 3,
            "tasks": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
            "errors": None,
        }

        dispatcher = TodoistToolDispatcher(mock_client, allow_mutations=True)
        result = dispatcher.execute_tool(
            "call_abc",
            "bulk_add_todoist_tasks",
            {"content": "test", "count": 3},
        )

        mock_client.bulk_add_todoist_tasks.assert_called_once_with({"content": "test", "count": 3})
        assert result["success"] is True
        assert result["tool_call_id"] == "call_abc"
        assert result["tool_name"] == "bulk_add_todoist_tasks"
        assert result["content"]["created_count"] == 3

    def test_blocked_when_mutations_disabled(self):
        """bulk_add is blocked by mutation guard."""
        mock_client = MagicMock()
        dispatcher = TodoistToolDispatcher(mock_client, allow_mutations=False)
        result = dispatcher.execute_tool(
            "call_1",
            "bulk_add_todoist_tasks",
            {"content": "test", "count": 5},
        )

        assert result["success"] is False
        assert result["mutation_blocked"] is True
        mock_client.bulk_add_todoist_tasks.assert_not_called()


class TestBulkAddResilience:
    """Verify BatchThrottle and BatchCircuitBreaker integration in bulk_add_todoist_tasks."""

    def _make_client(self):
        return TodoistApiClient(api_key="test-key")

    @patch("agents.agent_api.app.constants.EXECUTOR_MAX_WORKERS", 1)
    @patch("agents.agent_api.app.constants.EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", 2)
    @patch.object(TodoistApiClient, "_request")
    def test_circuit_breaker_trips_and_skips_remaining(self, mock_request):
        """After 2 consecutive transient failures, remaining requests are skipped."""
        mock_request.side_effect = TodoistApiError(
            kind="transient", message="Service unavailable", status_code=503
        )
        client = self._make_client()

        result = client.bulk_add_todoist_tasks({"content": "test", "count": 5})

        assert result["created_count"] == 0
        # Breaker trips after threshold (2), remaining get skipped
        assert mock_request.call_count == 2
        skipped = [e for e in result["errors"] if e.get("skipped")]
        assert len(skipped) == 3
        non_skipped = [e for e in result["errors"] if not e.get("skipped")]
        assert len(non_skipped) == 2

    @patch("agents.agent_api.app.constants.EXECUTOR_MAX_WORKERS", 1)
    @patch("agents.agent_api.app.constants.EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", 2)
    @patch.object(TodoistApiClient, "_request")
    def test_throttle_signals_backoff_on_rate_limit(self, mock_request):
        """A 429 with retry_after causes subsequent requests to pause."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise TodoistApiError(
                    kind="rate-limit", message="Rate limited", status_code=429, retry_after_seconds=0.3
                )
            return {"id": f"task_{call_count[0]}", "content": "test"}

        mock_request.side_effect = side_effect
        client = self._make_client()

        start = time.monotonic()
        result = client.bulk_add_todoist_tasks({"content": "test", "count": 3})
        elapsed = time.monotonic() - start

        # First call fails with 429, triggers 0.3s backoff for remaining calls
        assert result["created_count"] == 2
        assert len(result["errors"]) == 1
        assert "Rate limited" in result["errors"][0]["error"]
        # Elapsed should be >= 0.3s due to throttle backoff
        assert elapsed >= 0.25

    @patch("agents.agent_api.app.tools.todoist.client.EXECUTOR_MAX_WORKERS", 1)
    @patch("agents.agent_api.app.tools.todoist.client.EXECUTOR_BATCH_TIMEOUT_SECONDS", 0.1)
    @patch.object(TodoistApiClient, "_request")
    def test_batch_timeout_cancels_slow_requests(self, mock_request):
        """Requests exceeding the batch deadline get timeout errors."""
        call_count = [0]

        def slow_request(*args, **kwargs):
            call_count[0] += 1
            time.sleep(0.15)
            return {"id": str(call_count[0]), "content": "test"}

        mock_request.side_effect = slow_request
        client = self._make_client()

        result = client.bulk_add_todoist_tasks({"content": "test", "count": 5})

        # Batch deadline is 0.1s; with max_workers=1 and 0.15s per call,
        # the first call completes after the deadline, so remaining are timed out.
        timed_out = [e for e in (result["errors"] or []) if e.get("timeout")]
        assert len(timed_out) > 0
        assert result["created_count"] < 5

    @patch("agents.agent_api.app.constants.EXECUTOR_MAX_WORKERS", 1)
    @patch("agents.agent_api.app.constants.EXECUTOR_CIRCUIT_BREAKER_THRESHOLD", 2)
    @patch.object(TodoistApiClient, "_request")
    def test_mixed_success_then_breaker_trip(self, mock_request):
        """Successes reset breaker; consecutive failures after that trip it."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                return {"id": f"task_{call_count[0]}", "content": "test"}
            raise TodoistApiError(
                kind="transient", message="Service unavailable", status_code=503
            )

        mock_request.side_effect = side_effect
        client = self._make_client()

        result = client.bulk_add_todoist_tasks({"content": "test", "count": 6})

        # First 2 succeed, next 2 fail (trips breaker), remaining 2 skipped
        assert result["created_count"] == 2
        assert mock_request.call_count == 4
        skipped = [e for e in result["errors"] if e.get("skipped")]
        assert len(skipped) == 2
