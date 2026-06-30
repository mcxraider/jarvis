"""Tests for the prior-read entity index, shared extractor, and entity metadata."""

import json

from agents.agent_api.app.graph.entity_index import SeenEntityIndex
from agents.agent_api.app.graph.extractors import extract_task_items
from agents.agent_api.app.tools.metadata import entity_requirements


def _tool_call(name: str, call_id: str = "call_1", args=None) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args or {})}}


def _result(content, tool_name: str = "get_tasks", success: bool = True) -> dict:
    """A Jarvis tool-result envelope as stored in state['tool_results']."""
    return {
        "tool_call_id": "read_call",
        "tool_name": tool_name,
        "success": success,
        "content": content,
        "error": None,
        "mutation_blocked": False,
        "classified_error": None,
    }


class TestExtractTaskItems:
    def test_bare_list(self):
        items = extract_task_items([{"id": "t1"}, {"id": "t2"}, "junk"])
        assert [t["id"] for t in items] == ["t1", "t2"]

    def test_results_shape(self):
        items = extract_task_items({"results": [{"id": "t1"}], "next_cursor": None})
        assert [t["id"] for t in items] == ["t1"]

    def test_items_shape(self):
        items = extract_task_items({"items": [{"id": "c1"}], "next_cursor": None})
        assert [t["id"] for t in items] == ["c1"]

    def test_single_task(self):
        # A single task's own "content" field is a title string, not a nested envelope.
        items = extract_task_items({"id": "t9", "content": "Submit report"})
        assert items == [{"id": "t9", "content": "Submit report"}]

    def test_full_envelope_recurses_into_content(self):
        envelope = _result({"results": [{"id": "t1"}, {"id": "t2"}]})
        assert [t["id"] for t in extract_task_items(envelope)] == ["t1", "t2"]

    def test_junk_returns_empty(self):
        assert extract_task_items(None) == []
        assert extract_task_items("nope") == []
        assert extract_task_items({"unrelated": 1}) == []

    def test_task_with_dict_content_not_recursed(self):
        # A task whose "content" is a dict (e.g. future nested payload) must be
        # returned as-is, not recursed into.
        task = {"id": "t1", "content": {"note": "some nested value"}}
        items = extract_task_items(task)
        assert items == [task]

    def test_envelope_with_empty_inner_not_misidentified(self):
        # An envelope whose inner content is an empty list should return [] —
        # the envelope dict itself must not be returned as a task.
        envelope = {
            "tool_call_id": "c1",
            "tool_name": "get_tasks",
            "success": True,
            "content": [],
        }
        assert extract_task_items(envelope) == []


class TestSeenEntityIndexHas:
    def test_ids_from_list_result_are_seen(self):
        index = SeenEntityIndex([_result([{"id": "t1"}, {"id": "t2"}])])
        assert index.has("task", "t1")
        assert index.has("task", "t2")
        assert not index.has("task", "ghost")

    def test_single_task_result(self):
        index = SeenEntityIndex([_result({"id": "t9"}, tool_name="get_todoist_task")])
        assert index.has("task", "t9")

    def test_completed_items_result(self):
        index = SeenEntityIndex(
            [_result({"items": [{"id": "c1"}]}, tool_name="get_completed_todoist_tasks_by_completion_date")]
        )
        assert index.has("task", "c1")

    def test_failed_result_contributes_nothing(self):
        index = SeenEntityIndex([_result([{"id": "t1"}], success=False)])
        assert not index.has("task", "t1")

    def test_numeric_ids_match_as_strings(self):
        index = SeenEntityIndex([_result([{"id": 123}])])
        assert index.has("task", "123")
        assert index.has("task", 123)


class TestSeenEntityIndexViolations:
    def test_seen_mutation_has_no_violation(self):
        index = SeenEntityIndex([_result([{"id": "t1"}])])
        assert index.violations([_tool_call("complete_task", args={"task_id": "t1"})]) == []

    def test_unseen_mutation_is_a_violation(self):
        index = SeenEntityIndex([])
        violations = index.violations([_tool_call("complete_task", "c1", {"task_id": "ghost"})])
        assert len(violations) == 1
        assert violations[0]["tool_call_id"] == "c1"
        assert violations[0]["arg"] == "task_id"
        assert violations[0]["value"] == "ghost"

    def test_add_comment_without_task_id_skipped(self):
        index = SeenEntityIndex([])
        calls = [_tool_call("add_comment", args={"content": "hi", "project_id": "p1"})]
        assert index.violations(calls) == []

    def test_add_comment_with_unseen_task_id_is_violation(self):
        index = SeenEntityIndex([])
        calls = [_tool_call("add_comment", args={"content": "hi", "task_id": "ghost"})]
        assert len(index.violations(calls)) == 1

    def test_read_and_create_tools_have_no_requirements(self):
        index = SeenEntityIndex([])
        calls = [
            _tool_call("get_tasks"),
            _tool_call("add_todoist_task", args={"content": "x"}),
        ]
        assert index.violations(calls) == []

    def test_only_unseen_call_in_a_batch_is_flagged(self):
        index = SeenEntityIndex([_result([{"id": "t1"}])])
        calls = [
            _tool_call("complete_task", "c1", {"task_id": "t1"}),     # seen
            _tool_call("delete_todoist_task", "c2", {"task_id": "x"}),  # unseen
        ]
        violations = index.violations(calls)
        assert [v["tool_call_id"] for v in violations] == ["c2"]


class TestEntityRequirements:
    def test_mutators_require_task_id(self):
        for name in (
            "complete_task",
            "uncomplete_task",
            "update_todoist_task",
            "delete_todoist_task",
        ):
            refs = entity_requirements(name)
            assert len(refs) == 1
            assert refs[0].arg == "task_id"
            assert refs[0].entity_type == "task"
            assert refs[0].required is True

    def test_add_comment_task_id_is_optional(self):
        refs = entity_requirements("add_comment")
        assert refs[0].arg == "task_id"
        assert refs[0].required is False

    def test_reads_and_creates_have_no_requirements(self):
        for name in ("get_tasks", "get_todoist_task", "add_todoist_task", "ask_user"):
            assert entity_requirements(name) == ()
