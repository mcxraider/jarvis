"""Tests for the summarize node — extraction, validation, dynamic tokens, user query, fallback."""

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.agent_api.app.graph.extractors import extract_list_from_content
from agents.agent_api.app.graph.nodes.summarize import (
    _compute_max_tokens,
    _compute_min_coverage,
    _extract_task_ids,
    _is_count_query,
    _is_homogeneous,
    _truncate_fallback,
    _validate_summary,
    create_summarize_node,
)


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
    response = MagicMock()
    response.choices = [choice]
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
        result = node(state)
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

        node = create_summarize_node()
        state = _make_state(items)
        result = node(state)

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

        node = create_summarize_node()
        state = _make_state(items, user_prompt="what's due today?")
        node(state)

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

        node = create_summarize_node()
        state = _make_state(items)
        result = node(state)

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

        node = create_summarize_node()
        state = _make_state(items)
        result = node(state)

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

        node = create_summarize_node()
        state = _make_state(items)
        result = node(state)

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

        node = create_summarize_node()
        state = _make_state(items, user_prompt="how many tasks do I have today?")
        result = node(state)

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

        node = create_summarize_node()
        state = _make_state(items, user_prompt="show my tasks")
        result = node(state)

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

        node = create_summarize_node()
        state = _make_state(items, user_prompt="how many tasks total?")
        result = node(state)

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

        node = create_summarize_node()
        state = _make_state(items, user_prompt="show me all my tasks")
        result = node(state)

        mock_client.chat.completions.create.assert_called()
        tool_msg = result["messages"][-1]
        parsed = json.loads(tool_msg["content"])
        assert parsed["summarized"] is True
