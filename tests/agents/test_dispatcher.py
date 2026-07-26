"""Comprehensive unit tests for the ToolDispatcher."""

import json
import os

import pytest

# Disable tracing before importing anything that touches LangSmith/LangChain.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.dispatcher import ToolDispatcher, build_tool_result
from agents.agent_api.app.tools.todoist.client import TodoistApiError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _get_tasks_handler(arguments):
    """Read-only handler that returns a task list."""
    return [{"id": "1", "content": "Test"}]


def _add_task_handler(arguments):
    """Mutating handler that returns a new task id."""
    return {"id": "new_1"}


def _fail_tool_handler(arguments):
    """Handler that always raises a RuntimeError."""
    raise RuntimeError("Something broke")


def _todoist_error_handler(arguments):
    """Handler that raises a TodoistApiError."""
    raise TodoistApiError(
        kind="transient",
        message="Todoist is temporarily unavailable. Please try again shortly.",
        status_code=503,
        retryable=True,
        operation="todoist.get_tasks",
        method="GET",
        attempts=3,
        provider_message="labels must be an array of labels",
        provider_code=42,
        provider_tag="BAD_REQUEST",
    )


@pytest.fixture
def registry():
    """Build a ToolRegistry with read-only, mutating, failing, and TodoistApiError tools."""
    reg = ToolRegistry()
    reg.register([
        ToolSpec(
            name="get_tasks",
            openai_schema={"type": "function", "function": {"name": "get_tasks"}},
            handler=_get_tasks_handler,
            mutating=False,
        ),
        ToolSpec(
            name="add_task",
            openai_schema={"type": "function", "function": {"name": "add_task"}},
            handler=_add_task_handler,
            mutating=True,
        ),
        ToolSpec(
            name="fail_tool",
            openai_schema={"type": "function", "function": {"name": "fail_tool"}},
            handler=_fail_tool_handler,
            mutating=False,
        ),
        ToolSpec(
            name="todoist_error_tool",
            openai_schema={"type": "function", "function": {"name": "todoist_error_tool"}},
            handler=_todoist_error_handler,
            mutating=False,
        ),
    ])
    return reg


@pytest.fixture
def dispatcher(registry):
    """Dispatcher with mutations enabled."""
    return ToolDispatcher(registry, allow_mutations=True)


@pytest.fixture
def readonly_dispatcher(registry):
    """Dispatcher with mutations disabled."""
    return ToolDispatcher(registry, allow_mutations=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    """Tests for argument parsing within execute_tool_call."""

    def test_execute_tool_call_valid_json_arguments(self, dispatcher):
        """JSON string arguments are parsed and passed to the handler."""
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "get_tasks",
                "arguments": '{"task": "buy milk"}',
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is True
        assert result["tool_call_id"] == "call_1"
        assert result["tool_name"] == "get_tasks"
        assert result["content"] == [{"id": "1", "content": "Test"}]
        assert result["error"] is None

    def test_execute_tool_call_dict_arguments(self, dispatcher):
        """Dict arguments are passed directly without JSON parsing."""
        tool_call = {
            "id": "call_2",
            "function": {
                "name": "get_tasks",
                "arguments": {"task": "buy milk"},
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is True
        assert result["tool_call_id"] == "call_2"
        assert result["tool_name"] == "get_tasks"
        assert result["content"] == [{"id": "1", "content": "Test"}]

    def test_execute_tool_call_empty_string_arguments(self, dispatcher):
        """Empty string arguments default to empty dict."""
        tool_call = {
            "id": "call_3",
            "function": {
                "name": "get_tasks",
                "arguments": "",
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is True
        assert result["tool_name"] == "get_tasks"
        assert result["content"] == [{"id": "1", "content": "Test"}]

    def test_execute_tool_call_malformed_json(self, dispatcher):
        """Malformed JSON returns an error result without crashing."""
        tool_call = {
            "id": "call_4",
            "function": {
                "name": "get_tasks",
                "arguments": "not{json",
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is False
        assert result["tool_call_id"] == "call_4"
        assert result["tool_name"] == "get_tasks"
        assert result["error"] is not None
        assert result["content"] is None


class TestToolRouting:
    """Tests for tool name resolution and unknown tool handling."""

    def test_execute_tool_unknown_tool(self, dispatcher):
        """Unknown tool name returns an Unsupported tool error."""
        tool_call = {
            "id": "call_5",
            "function": {
                "name": "xyz",
                "arguments": "{}",
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is False
        assert result["tool_call_id"] == "call_5"
        assert result["tool_name"] == "xyz"
        assert "Unsupported tool: xyz" in result["error"]


class TestMutationGuard:
    """Tests for the mutation guard logic."""

    def test_mutation_guard_blocks_when_disabled(self, readonly_dispatcher):
        """Mutating tool is blocked when allow_mutations=False."""
        tool_call = {
            "id": "call_6",
            "function": {
                "name": "add_task",
                "arguments": '{"content": "new task"}',
            },
        }
        result = readonly_dispatcher.execute_tool_call(tool_call)

        assert result["success"] is False
        assert result["tool_name"] == "add_task"
        assert result["mutation_blocked"] is True
        assert "Mutation blocked for add_task" in result["error"]

    def test_mutation_guard_allows_when_enabled(self, dispatcher):
        """Mutating tool succeeds when allow_mutations=True."""
        tool_call = {
            "id": "call_7",
            "function": {
                "name": "add_task",
                "arguments": '{"content": "new task"}',
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is True
        assert result["tool_name"] == "add_task"
        assert result["content"] == {"id": "new_1"}
        assert result["mutation_blocked"] is False

    def test_mutation_guard_allows_readonly_regardless(self, readonly_dispatcher):
        """Read-only tool passes even when allow_mutations=False."""
        tool_call = {
            "id": "call_8",
            "function": {
                "name": "get_tasks",
                "arguments": "{}",
            },
        }
        result = readonly_dispatcher.execute_tool_call(tool_call)

        assert result["success"] is True
        assert result["tool_name"] == "get_tasks"
        assert result["content"] == [{"id": "1", "content": "Test"}]
        assert result["mutation_blocked"] is False


class TestErrorHandling:
    """Tests for error handling paths."""

    def test_todoist_api_error_produces_classified_result(self, dispatcher):
        """TodoistApiError is caught and classified_error is populated."""
        tool_call = {
            "id": "call_9",
            "function": {
                "name": "todoist_error_tool",
                "arguments": "{}",
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is False
        assert result["tool_name"] == "todoist_error_tool"
        assert result["error"] == "Todoist is temporarily unavailable. Please try again shortly."
        assert result["classified_error"] is not None
        assert result["classified_error"]["source"] == "todoist"
        assert result["classified_error"]["kind"] == "transient"
        assert result["classified_error"]["retryable"] is True
        assert result["classified_error"]["attempts"] == 3
        assert result["classified_error"]["status_code"] == 503
        assert result["classified_error"]["method"] == "GET"
        assert result["classified_error"]["operation"] == "todoist.get_tasks"
        assert (
            result["classified_error"]["provider_message"]
            == "labels must be an array of labels"
        )
        assert result["classified_error"]["provider_code"] == 42
        assert result["classified_error"]["provider_tag"] == "BAD_REQUEST"

    def test_generic_exception_produces_error_result(self, dispatcher):
        """RuntimeError is caught and the error field contains the message."""
        tool_call = {
            "id": "call_10",
            "function": {
                "name": "fail_tool",
                "arguments": "{}",
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["success"] is False
        assert result["tool_name"] == "fail_tool"
        assert result["error"] == "Something broke"
        assert result["content"] is None
        assert result["classified_error"] is None


class TestBatchExecution:
    """Tests for execute_tool_calls batch method."""

    def test_execute_tool_calls_batch_preserves_order(self, dispatcher):
        """Batch of 3 calls returns results in the same order."""
        tool_calls = [
            {
                "id": "call_a",
                "function": {"name": "get_tasks", "arguments": "{}"},
            },
            {
                "id": "call_b",
                "function": {"name": "add_task", "arguments": '{"content": "hi"}'},
            },
            {
                "id": "call_c",
                "function": {"name": "get_tasks", "arguments": "{}"},
            },
        ]
        results = dispatcher.execute_tool_calls(tool_calls)

        assert len(results) == 3
        assert results[0]["tool_call_id"] == "call_a"
        assert results[0]["tool_name"] == "get_tasks"
        assert results[0]["success"] is True
        assert results[1]["tool_call_id"] == "call_b"
        assert results[1]["tool_name"] == "add_task"
        assert results[1]["success"] is True
        assert results[2]["tool_call_id"] == "call_c"
        assert results[2]["tool_name"] == "get_tasks"
        assert results[2]["success"] is True


class TestEdgeCases:
    """Tests for missing keys and edge-case tool call shapes."""

    def test_missing_function_key_uses_defaults(self, dispatcher):
        """A tool_call without the 'function' key uses defaults and does not crash."""
        tool_call = {"id": "call_11"}
        result = dispatcher.execute_tool_call(tool_call)

        # Without function key, tool_name defaults to "unknown" which is unsupported
        assert result["success"] is False
        assert result["tool_call_id"] == "call_11"
        assert result["tool_name"] == "unknown"
        assert "Unsupported tool: unknown" in result["error"]

    def test_missing_id_uses_sentinel(self, dispatcher):
        """A tool_call without 'id' uses 'missing_tool_call_id' as the sentinel."""
        tool_call = {
            "function": {
                "name": "get_tasks",
                "arguments": "{}",
            },
        }
        result = dispatcher.execute_tool_call(tool_call)

        assert result["tool_call_id"] == "missing_tool_call_id"
        assert result["success"] is True
        assert result["tool_name"] == "get_tasks"
