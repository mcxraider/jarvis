"""Unit tests for the tool-call parallelism tree renderer."""

import pytest

from agents.agent_api.app.formatting.tool_tree import render_tool_tree


def _result(tool_name="test_tool", success=True, batch_index=None, service="", error=None, mutation_blocked=False):
    return {
        "tool_call_id": "call_1",
        "tool_name": tool_name,
        "success": success,
        "content": None,
        "error": error,
        "mutation_blocked": mutation_blocked,
        "batch_index": batch_index,
        "service": service,
    }


class TestRenderToolTree:
    def test_empty_results(self):
        assert render_tool_tree([]) == "None"

    def test_no_batch_index_falls_back_to_flat(self):
        results = [
            _result("get_tasks", success=True),
            _result("delete_task", success=False, error="not found"),
        ]
        output = render_tool_tree(results)
        assert "1. get_tasks [ok]" in output
        assert "2. delete_task [error]" in output
        assert "not found" in output

    def test_single_batch_single_item(self):
        results = [_result("get_tasks", batch_index=0, service="todoist")]
        output = render_tool_tree(results)
        assert "─ get_tasks [todoist ✅]" in output

    def test_single_batch_multiple_items_parallel(self):
        results = [
            _result("get_tasks_by_filter", batch_index=0, service="todoist"),
            _result("get_tasks_by_filter", batch_index=0, service="todoist"),
        ]
        output = render_tool_tree(results)
        lines = output.split("\n")
        assert lines[0].startswith("┬")
        assert lines[1].startswith("└")

    def test_single_batch_three_items(self):
        results = [
            _result("tool_a", batch_index=0, service="todoist"),
            _result("tool_b", batch_index=0, service="todoist"),
            _result("tool_c", batch_index=0, service="todoist"),
        ]
        output = render_tool_tree(results)
        lines = output.split("\n")
        assert lines[0].startswith("┬")
        assert lines[1].startswith("├")
        assert lines[2].startswith("└")

    def test_multiple_batches_sequential(self):
        results = [
            _result("get_tasks_by_filter", batch_index=0, service="todoist"),
            _result("get_tasks_by_filter", batch_index=0, service="todoist"),
            _result("delete_todoist_task", batch_index=1, service="todoist"),
            _result("delete_todoist_task", batch_index=1, service="todoist"),
        ]
        output = render_tool_tree(results)
        lines = output.split("\n")
        assert "┬" in lines[0]
        assert "└" in lines[1]
        assert lines[2] == "│"
        assert "┬" in lines[3]
        assert "└" in lines[4]

    def test_failure_icon(self):
        results = [_result("bad_call", batch_index=0, service="todoist", success=False)]
        output = render_tool_tree(results)
        assert "❌" in output
        assert "✅" not in output

    def test_mutation_blocked_icon(self):
        results = [_result("delete_task", batch_index=0, service="todoist", mutation_blocked=True)]
        output = render_tool_tree(results)
        assert "⛔" in output

    def test_error_detail_indented(self):
        results = [
            _result("tool_a", batch_index=0, service="todoist", success=False, error="timeout"),
            _result("tool_b", batch_index=0, service="todoist"),
        ]
        output = render_tool_tree(results)
        assert "↳ timeout" in output

    def test_no_service_omits_label(self):
        results = [_result("ask_user", batch_index=0, service="")]
        output = render_tool_tree(results)
        assert "─ ask_user [✅]" in output

    def test_mixed_batches_with_single_and_parallel(self):
        results = [
            _result("get_tasks", batch_index=0, service="todoist"),
            _result("delete_task", batch_index=1, service="todoist"),
            _result("delete_task", batch_index=1, service="todoist"),
        ]
        output = render_tool_tree(results)
        lines = output.split("\n")
        assert lines[0].startswith("─")
        assert lines[1] == "│"
        assert lines[2].startswith("┬")
        assert lines[3].startswith("└")
