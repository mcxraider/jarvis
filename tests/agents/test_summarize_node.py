"""Tests for the summarize node — extraction, validation, dynamic tokens, user query, fallback."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.agent_api.app.graph.extractors import extract_list_from_content
from agents.agent_api.app.graph.nodes import summarize as summarize_module
from agents.agent_api.app.graph.nodes.summarize import (
    _compute_max_tokens,
    _compute_min_coverage,
    _extract_task_ids,
    _is_count_query,
    _is_homogeneous,
    _truncate_fallback,
    _validate_summary,
    close_shared_async_summarizer_client,
    close_shared_summarizer_client,
    create_summarize_node,
    get_shared_async_summarizer_client,
    get_shared_summarizer_client,
)
from agents.agent_api.app.graph.run_deps import CONFIGURABLE_DEPS_KEY, RunDeps
from agents.agent_api.app.llm.chat import UsageLedger
from agents.agent_api.app.llm.provider import OpenAIChatProfile


@pytest.fixture(autouse=True)
def _reset_shared_summarizer_clients():
    close_shared_summarizer_client()
    asyncio.run(close_shared_async_summarizer_client())
    yield
    close_shared_summarizer_client()
    asyncio.run(close_shared_async_summarizer_client())


@pytest.fixture(autouse=True)
def _patch_summarize_wrap_openai():
    with patch(
        "agents.agent_api.app.graph.nodes.summarize.wrap_openai",
        side_effect=lambda c, **_: c,
    ):
        yield


# --- Extraction tests ---


class TestExtractListFromContent:
    def test_handles_bare_list(self):
        items = [{"id": "1"}, {"id": "2"}]
        assert extract_list_from_content(items) == items

    def test_handles_tasks_key(self):
        data = {"tasks": [{"id": "1"}, {"id": "2"}]}
        assert extract_list_from_content(data) == [{"id": "1"}, {"id": "2"}]

    def test_handles_results_key(self):
        data = {"results": [{"id": "1"}]}
        assert extract_list_from_content(data) == [{"id": "1"}]

    def test_handles_items_key(self):
        data = {"items": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
        assert extract_list_from_content(data) == [{"id": "1"}, {"id": "2"}, {"id": "3"}]

    def test_returns_none_for_string(self):
        assert extract_list_from_content("some string") is None

    def test_returns_none_for_dict_without_known_keys(self):
        assert extract_list_from_content({"foo": [1, 2, 3]}) is None

    def test_returns_none_for_none(self):
        assert extract_list_from_content(None) is None

    def test_priority_order_results_first(self):
        data = {"results": [1, 2], "tasks": [3, 4], "items": [5, 6]}
        assert extract_list_from_content(data) == [1, 2]


# --- Validation tests ---


def _make_items(count: int) -> list:
    return [{"id": str(i), "content": f"Task {i}"} for i in range(1, count + 1)]


class TestValidateSummary:
    def test_passes_when_all_ids_present(self):
        items = _make_items(10)
        ids = _extract_task_ids(items)
        summary = "Tasks: " + ", ".join(f"ID {i}" for i in range(1, 11))
        assert _validate_summary(summary, ids) is True

    def test_passes_at_exactly_70_percent(self):
        items = _make_items(10)
        ids = _extract_task_ids(items)
        summary = "Tasks: " + ", ".join(f"ID {i}" for i in range(1, 8))
        assert _validate_summary(summary, ids) is True

    def test_fails_below_70_percent(self):
        items = _make_items(10)
        ids = _extract_task_ids(items)
        summary = "Tasks: " + ", ".join(f"ID {i}" for i in range(1, 5))
        assert _validate_summary(summary, ids) is False

    def test_passes_with_no_ids_in_input(self):
        items = [{"content": "no id here"}]
        ids = _extract_task_ids(items)
        assert _validate_summary("anything", ids) is True

    def test_empty_ids_set_passes(self):
        assert _validate_summary("anything", set()) is True


# --- Dynamic max_tokens tests ---


class TestComputeMaxTokens:
    def test_small_list(self):
        assert _compute_max_tokens(10) == 500 + 10 * 50  # 1000

    def test_medium_list(self):
        assert _compute_max_tokens(50) == 500 + 50 * 50  # 3000

    def test_large_list_capped(self):
        assert _compute_max_tokens(400) == 15000

    def test_exactly_at_ceiling(self):
        # (15000 - 500) / 50 = 290 items to hit ceiling
        assert _compute_max_tokens(290) == 15000


# --- Truncate fallback tests ---


class TestTruncateFallback:
    def test_shows_first_n_items(self):
        items = _make_items(50)
        result = _truncate_fallback(items, 20)
        assert "50 total items" in result
        assert "showing first 20" in result
        parsed_part = json.loads(result.split("\n")[0])
        assert len(parsed_part) == 20


# --- Node integration tests ---


def _make_mock_response(content: str):
    """Build a mock OpenAI completion response."""
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    choice.message.refusal = None
    choice.message.reasoning_content = None
    choice.finish_reason = "stop"
    response = MagicMock()
    response.choices = [choice]
    response.usage = None
    response.model = "deepseek-v4-flash"
    response._request_id = None
    response.request_id = None
    return response


def _make_tool_message(items: list, tool_name: str = "get_tasks") -> dict:
    """Build a tool-role message as it appears in state['messages']."""
    envelope = {
        "tool_call_id": "call_1",
        "tool_name": tool_name,
        "success": True,
        "content": items,
        "error": None,
    }
    return {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": tool_name,
        "content": json.dumps(envelope, default=str),
    }


def _make_state(items: list, user_prompt: str = "show my tasks", tool_name: str = "get_tasks") -> dict:
    """Build a minimal JarvisState with one tool message exceeding threshold."""
    return {
        "messages": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            _make_tool_message(items, tool_name),
        ],
        "user_prompt": user_prompt,
        "tool_results": [{"tool_call_id": "call_1", "content": items}],
    }


class TestSummarizeNode:
    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_skips_below_threshold(self, mock_openai_cls):
        node = create_summarize_node()
        items = _make_items(15)
        state = _make_state(items)
        result = asyncio.run(node(state))
        mock_openai_cls.return_value.chat.completions.create.assert_not_called()
        # Messages should still be returned (deep copied)
        assert result["next"] == "agent"

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_processes_above_threshold(self, mock_openai_cls):
        items = _make_items(55)
        all_ids = " ".join(str(i) for i in range(1, 56))
        summary = f"Summary with IDs: {all_ids}\nTOTAL: 55 tasks | DETAILED: 55 | ABBREVIATED: 0 | OMITTED: 0"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_response(summary)
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items)
        result = asyncio.run(node(state))

        mock_client.chat.completions.create.assert_called()
        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert parsed["summarized"] is True
        assert parsed["original_item_count"] == 55
        assert "Summary with IDs" in parsed["content"]

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_user_query_passed_to_prompt(self, mock_openai_cls):
        items = _make_items(55)
        all_ids = " ".join(str(i) for i in range(1, 56))
        summary = f"IDs: {all_ids}"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_response(summary)
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items, user_prompt="what's due today?")
        asyncio.run(node(state))

        call_args = mock_client.chat.completions.create.call_args
        messages_sent = call_args.kwargs.get("messages") or call_args[1].get("messages")
        user_msg = next(m for m in messages_sent if m["role"] == "user")
        assert "what's due today?" in user_msg["content"]

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_validation_triggers_retry(self, mock_openai_cls):
        items = _make_items(55)
        bad_summary = "Only ID 1 and 2 here"
        all_ids = " ".join(str(i) for i in range(1, 56))
        good_summary = f"All IDs: {all_ids}"

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _make_mock_response(bad_summary),
            _make_mock_response(good_summary),
        ]
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items)
        result = asyncio.run(node(state))

        assert mock_client.chat.completions.create.call_count == 2
        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert "All IDs" in parsed["content"]

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_fallback_on_double_validation_failure(self, mock_openai_cls):
        items = _make_items(55)
        bad_summary = "Only ID 1 here"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_response(bad_summary)
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items)
        result = asyncio.run(node(state))

        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert "55 total items" in parsed["content"]
        assert "showing first" in parsed["content"]

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_fallback_on_llm_timeout(self, mock_openai_cls):
        from openai import APITimeoutError

        items = _make_items(55)

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items)
        result = asyncio.run(node(state))

        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert "55 total items" in parsed["content"]


# --- Stage 3: Bypass and scaled coverage tests ---


class TestIsCountQuery:
    def test_detects_how_many(self):
        assert _is_count_query("How many tasks do I have today?") is True

    def test_detects_count(self):
        assert _is_count_query("count my overdue tasks") is True

    def test_detects_total_number(self):
        assert _is_count_query("what is the total number of tasks?") is True

    def test_detects_how_much(self):
        assert _is_count_query("how much work is pending?") is True

    def test_detects_number_of(self):
        assert _is_count_query("what's the number of items due?") is True

    def test_rejects_non_count_query(self):
        assert _is_count_query("show me my tasks for today") is False

    def test_case_insensitive(self):
        assert _is_count_query("HOW MANY tasks?") is True

    def test_empty_string(self):
        assert _is_count_query("") is False


class TestIsHomogeneous:
    def test_all_same_content(self):
        items = [{"id": str(i), "content": "hehehe"} for i in range(20)]
        assert _is_homogeneous(items) is True

    def test_exactly_at_threshold(self):
        items = [{"id": str(i), "content": "same"} for i in range(8)]
        items += [{"id": "9", "content": "different1"}, {"id": "10", "content": "different2"}]
        assert _is_homogeneous(items, threshold=0.8) is True

    def test_below_threshold(self):
        items = [{"id": str(i), "content": f"task {i}"} for i in range(10)]
        assert _is_homogeneous(items) is False

    def test_empty_list(self):
        assert _is_homogeneous([]) is False

    def test_non_dict_items_ignored(self):
        items = ["string1", "string2", "string3"]
        assert _is_homogeneous(items) is False

    def test_items_without_content_key(self):
        items = [{"id": str(i)} for i in range(10)]
        # All have "" as content (default from .get("content", ""))
        assert _is_homogeneous(items) is True


class TestComputeMinCoverage:
    def test_small_list_high_coverage(self):
        assert _compute_min_coverage(20) == 0.9

    def test_boundary_30(self):
        assert _compute_min_coverage(30) == 0.9

    def test_medium_list(self):
        assert _compute_min_coverage(50) == 0.7

    def test_boundary_75(self):
        assert _compute_min_coverage(75) == 0.7

    def test_large_list_low_coverage(self):
        assert _compute_min_coverage(100) == 0.5

    def test_very_large_list(self):
        assert _compute_min_coverage(200) == 0.5


class TestValidateSummaryWithMinCoverage:
    def test_custom_min_coverage_passes(self):
        items = _make_items(10)
        ids = _extract_task_ids(items)
        # Only 5/10 = 50% present
        summary = "Tasks: " + ", ".join(f"ID {i}" for i in range(1, 6))
        assert _validate_summary(summary, ids, min_coverage=0.5) is True

    def test_custom_min_coverage_fails(self):
        items = _make_items(10)
        ids = _extract_task_ids(items)
        summary = "Tasks: ID 1, ID 2, ID 3"
        assert _validate_summary(summary, ids, min_coverage=0.5) is False

    def test_none_min_coverage_uses_default(self):
        items = _make_items(10)
        ids = _extract_task_ids(items)
        # 7/10 = 70% — should pass with default 0.7
        summary = "Tasks: " + ", ".join(f"ID {i}" for i in range(1, 8))
        assert _validate_summary(summary, ids, min_coverage=None) is True


class TestSummarizeNodeBypass:
    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_bypass_count_query(self, mock_openai_cls):
        """Count queries with ≤100 items skip summarization entirely."""
        items = _make_items(55)

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items, user_prompt="how many tasks do I have today?")
        result = asyncio.run(node(state))

        mock_client.chat.completions.create.assert_not_called()
        assert result["next"] == "agent"
        # Message content should be unchanged (raw data passed through)
        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert "summarized" not in parsed

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_bypass_homogeneous_results(self, mock_openai_cls):
        """Homogeneous results skip summarization."""
        items = [{"id": str(i), "content": "hehehe"} for i in range(55)]

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items, user_prompt="show my tasks")
        result = asyncio.run(node(state))

        mock_client.chat.completions.create.assert_not_called()
        assert result["next"] == "agent"

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_count_query_above_100_still_summarizes(self, mock_openai_cls):
        """Count queries with >100 items still need summarization."""
        items = _make_items(105)
        all_ids = " ".join(str(i) for i in range(1, 106))
        summary = f"IDs: {all_ids}"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_response(summary)
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items, user_prompt="how many tasks total?")
        result = asyncio.run(node(state))

        mock_client.chat.completions.create.assert_called()
        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert parsed["summarized"] is True

    @patch("agents.agent_api.app.graph.nodes.summarize.OpenAI")
    def test_diverse_non_count_query_summarizes(self, mock_openai_cls):
        """Diverse results on non-count queries still go through summarizer."""
        items = _make_items(55)
        all_ids = " ".join(str(i) for i in range(1, 56))
        summary = f"IDs: {all_ids}"

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_mock_response(summary)
        mock_openai_cls.return_value = mock_client

        node = create_summarize_node(client=mock_client)
        state = _make_state(items, user_prompt="show me all my tasks")
        result = asyncio.run(node(state))

        mock_client.chat.completions.create.assert_called()
        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert parsed["summarized"] is True


class _RecordingTracer:
    def __init__(self):
        self.events = []

    def event(self, stage, message, **fields):
        self.events.append({"stage": stage, "message": message, "fields": fields})


class TestSharedSummarizerClients:
    def test_node_build_does_not_initialize_provider_transport(self):
        with (
            patch.object(summarize_module, "OpenAI") as openai_cls,
            patch.object(summarize_module, "AsyncOpenAI") as async_openai_cls,
        ):
            node = create_summarize_node()
            result = asyncio.run(node(_make_state(_make_items(10))))

        assert result["next"] == "agent"
        openai_cls.assert_not_called()
        async_openai_cls.assert_not_called()

    def test_sync_client_reused_across_node_builds(self):
        sdk_client = MagicMock()
        with patch.object(
            summarize_module, "OpenAI", return_value=sdk_client
        ) as openai_cls:
            first = get_shared_summarizer_client()
            second = get_shared_summarizer_client()
            first_node = create_summarize_node()
            second_node = create_summarize_node()

        assert first is second
        assert callable(first_node)
        assert callable(second_node)
        assert openai_cls.call_count == 1

    def test_async_client_reused(self):
        sdk_client = MagicMock()
        sdk_client.close = AsyncMock()
        with patch.object(
            summarize_module, "AsyncOpenAI", return_value=sdk_client
        ) as openai_cls:
            first = get_shared_async_summarizer_client()
            second = get_shared_async_summarizer_client()

        assert first is second
        assert openai_cls.call_count == 1

    def test_injected_async_client_uses_native_completion(self):
        sdk_client = MagicMock()
        all_ids = " ".join(str(index) for index in range(1, 56))
        sdk_client.chat.completions.create = AsyncMock(
            return_value=_make_mock_response(f"IDs: {all_ids}")
        )
        node = create_summarize_node(client=sdk_client)

        result = asyncio.run(node(_make_state(_make_items(55))))

        assert json.loads(result["messages"][-1]["content"])["summarized"] is True
        sdk_client.chat.completions.create.assert_awaited_once()

    def test_async_transport_initialization_failure_uses_fallback(self):
        with patch.object(
            summarize_module,
            "get_shared_async_summarizer_client",
            side_effect=RuntimeError("missing credentials"),
        ):
            result = asyncio.run(
                create_summarize_node()(_make_state(_make_items(55)))
            )

        content = json.loads(result["messages"][-1]["content"])["content"]
        assert "55 total items" in content

    def test_missing_run_tracer_uses_direct_call_fallback(self):
        tracer = _RecordingTracer()
        sdk_client = MagicMock()
        node = create_summarize_node(tracer, client=sdk_client)
        config = {
            "configurable": {
                CONFIGURABLE_DEPS_KEY: RunDeps(tracer=None),
            }
        }

        result = asyncio.run(node(_make_state(_make_items(10)), config))

        assert result["next"] == "agent"
        assert any(
            event["stage"] == "graph.summarize" for event in tracer.events
        )

    def test_compile_once_node_keeps_concurrent_run_tracers_isolated(self):
        sdk_client = MagicMock()
        barrier = threading.Barrier(2)
        calls = []
        calls_lock = threading.Lock()

        def create(**kwargs):
            with calls_lock:
                calls.append(kwargs)
            barrier.wait(timeout=5)
            all_ids = " ".join(str(index) for index in range(1, 61))
            return _make_mock_response(f"IDs: {all_ids}")

        sdk_client.chat.completions.create.side_effect = create
        first_tracer = _RecordingTracer()
        second_tracer = _RecordingTracer()
        node = create_summarize_node(client=sdk_client, model="gpt-5.6-luna")
        first_config = {
            "configurable": {
                CONFIGURABLE_DEPS_KEY: RunDeps(tracer=first_tracer),
            }
        }
        second_config = {
            "configurable": {
                CONFIGURABLE_DEPS_KEY: RunDeps(tracer=second_tracer),
            }
        }

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                asyncio.run,
                node(
                    _make_state(_make_items(55), "first"),
                    first_config,
                ),
            )
            second_future = pool.submit(
                asyncio.run,
                node(
                    _make_state(_make_items(60), "second"),
                    second_config,
                ),
            )
            first_result = first_future.result(timeout=5)
            second_result = second_future.result(timeout=5)

        assert json.loads(first_result["messages"][-1]["content"])["summarized"] is True
        assert json.loads(second_result["messages"][-1]["content"])["summarized"] is True
        assert {call["model"] for call in calls} == {"gpt-5.6-luna"}
        first_done = next(
            event
            for event in first_tracer.events
            if event["stage"] == "graph.summarize.llm_done"
        )
        second_done = next(
            event
            for event in second_tracer.events
            if event["stage"] == "graph.summarize.llm_done"
        )
        assert first_done["fields"]["item_count"] == 55
        assert second_done["fields"]["item_count"] == 60


def test_openai_summarizer_request_and_usage_are_provider_aware(monkeypatch):
    items = _make_items(55)
    summary = "IDs: " + " ".join(str(index) for index in range(1, 56))
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=summary,
                    tool_calls=None,
                    refusal=None,
                ),
                finish_reason="stop",
            )
        ],
        model="gpt-5.6-luna",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=25,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            service_tier=None,
        ),
        service_tier=None,
        id="req_test",
    )
    seen_kwargs = []

    async def create(**kwargs):
        seen_kwargs.append(kwargs)
        return response

    sdk_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    profile = OpenAIChatProfile(
        api_key="openai-test-key",
        base_url="https://api.openai.com/v1",
        model="gpt-5.6-luna",
        max_output_tokens=4_000,
        request_timeout_seconds=30.0,
        max_retry_attempts=3,
        retry_max_delay_seconds=8.0,
        sdk_max_retries=0,
    )
    monkeypatch.setattr(
        summarize_module,
        "settings",
        SimpleNamespace(llm_safety_identifier_secret="safety-secret"),
    )
    ledger = UsageLedger()
    config = {
        "configurable": {
            CONFIGURABLE_DEPS_KEY: RunDeps(usage_accumulator=ledger),
        }
    }
    state = _make_state(items)
    state["user_id"] = "user-123"

    result = asyncio.run(
        create_summarize_node(client=sdk_client, profile=profile)(state, config)
    )

    assert json.loads(result["messages"][-1]["content"])["summarized"] is True
    assert len(seen_kwargs) == 1
    kwargs = seen_kwargs[0]
    assert kwargs["model"] == "gpt-5.6-luna"
    assert kwargs["max_completion_tokens"] == _compute_max_tokens(55)
    assert kwargs["reasoning_effort"] == "none"
    assert len(kwargs["safety_identifier"]) == 64
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs
    assert "extra_body" not in kwargs
    assert len(ledger.calls) == 1
    assert ledger.calls[0].provider.value == "openai"
    assert ledger.calls[0].cached_read_tokens == 10


def test_wrap_openai_receives_summarize_span_names(monkeypatch):
    """wrap_openai is called with summarize.llm.<provider> span names."""
    spy = MagicMock(side_effect=lambda c, **_: c)
    with (
        patch("agents.agent_api.app.graph.nodes.summarize.wrap_openai", spy),
        patch("agents.agent_api.app.graph.nodes.summarize.OpenAI", return_value=MagicMock()),
    ):
        get_shared_summarizer_client()
    assert spy.called
    call_kwargs = spy.call_args[1]
    assert call_kwargs["chat_name"].startswith("summarize.llm.")
    assert call_kwargs["completions_name"].startswith("summarize.llm.")
    assert call_kwargs["chat_name"] == call_kwargs["completions_name"]
