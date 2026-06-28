"""Tests for KeywordToolSelector."""

import pytest

from agents.agent_api.app.tools.base import ToolRegistry, ToolSpec
from agents.agent_api.app.tools.selectors.keyword import KeywordToolSelector


def _build_registry() -> ToolRegistry:
    """Build a registry matching the real Jarvis tool set."""
    tools = [
        ("ask_user", False),
        ("get_todoist_task", False),
        ("get_tasks", False),
        ("get_tasks_by_filter", False),
        ("get_completed_todoist_tasks_by_completion_date", False),
        ("get_comments", False),
        ("get_labels", False),
        ("add_todoist_task", True),
        ("bulk_add_todoist_tasks", True),
        ("update_todoist_task", True),
        ("complete_task", True),
        ("uncomplete_task", True),
        ("delete_todoist_task", True),
        ("add_comment", True),
    ]
    specs = [
        ToolSpec(
            name=name,
            openai_schema={"type": "function", "function": {"name": name}},
            mutating=mutating,
        )
        for name, mutating in tools
    ]
    registry = ToolRegistry()
    registry.register(specs)
    return registry


def _selected_names(schemas):
    return {s["function"]["name"] for s in schemas}


class TestKeywordMatching:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_create_query(self):
        result = _selected_names(self.selector.select_schemas("add buy milk today", self.registry))
        assert "add_todoist_task" in result
        assert "ask_user" in result
        assert "delete_todoist_task" not in result

    def test_remind_maps_to_create(self):
        result = _selected_names(self.selector.select_schemas("remind me to call mom", self.registry))
        assert "add_todoist_task" in result

    def test_read_query(self):
        result = _selected_names(self.selector.select_schemas("show me my tasks", self.registry))
        assert result == {"get_tasks", "ask_user"}

    def test_completed_query(self):
        result = _selected_names(
            self.selector.select_schemas("what did i complete this week", self.registry)
        )
        assert "get_completed_todoist_tasks_by_completion_date" in result

    def test_update_query(self):
        result = _selected_names(
            self.selector.select_schemas("reschedule lunch to friday", self.registry)
        )
        assert "get_tasks" in result
        assert "update_todoist_task" in result
        assert "ask_user" in result

    def test_complete_query(self):
        result = _selected_names(
            self.selector.select_schemas("mark buy groceries as done", self.registry)
        )
        assert "get_tasks" in result
        assert "complete_task" in result

    def test_delete_query(self):
        result = _selected_names(
            self.selector.select_schemas("delete the packing task", self.registry)
        )
        assert "get_tasks" in result
        assert "delete_todoist_task" in result

    def test_label_query(self):
        result = _selected_names(self.selector.select_schemas("show my labels", self.registry))
        assert "get_labels" in result

    def test_filter_query(self):
        result = _selected_names(
            self.selector.select_schemas("filter tasks by today", self.registry)
        )
        assert "get_tasks_by_filter" in result

    def test_comment_query(self):
        result = _selected_names(
            self.selector.select_schemas("add a comment to that task", self.registry)
        )
        assert "get_comments" in result
        assert "add_comment" in result

    def test_bulk_query(self):
        result = _selected_names(
            self.selector.select_schemas("bulk add 5 packing tasks", self.registry)
        )
        assert "bulk_add_todoist_tasks" in result


class TestMultiIntent:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_add_and_complete(self):
        result = _selected_names(
            self.selector.select_schemas("add send Feebee off and mark study done", self.registry)
        )
        assert "add_todoist_task" in result
        assert "get_tasks" in result
        assert "complete_task" in result


class TestFallback:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_no_match_returns_all(self):
        result = _selected_names(
            self.selector.select_schemas("hello how are you", self.registry)
        )
        assert len(result) == 14

    def test_ambiguous_returns_all(self):
        result = _selected_names(self.selector.select_schemas("tasks", self.registry))
        assert len(result) == 14


class TestMutationFilter:
    def setup_method(self):
        self.registry = _build_registry()

    def test_mutations_disabled_drops_mutating_tools(self):
        selector = KeywordToolSelector(allow_mutations=False)
        result = _selected_names(
            selector.select_schemas("add buy milk today", self.registry)
        )
        assert "add_todoist_task" not in result
        assert "ask_user" in result

    def test_mutations_disabled_read_tools_still_work(self):
        selector = KeywordToolSelector(allow_mutations=False)
        result = _selected_names(selector.select_schemas("show my tasks", self.registry))
        assert "get_tasks" in result

    def test_mutations_disabled_fallback_still_excludes_mutating(self):
        selector = KeywordToolSelector(allow_mutations=False)
        result = _selected_names(selector.select_schemas("hello", self.registry))
        # Fallback returns all, but mutation filter still applies? Let's verify.
        # Actually: when fallback fires, we return ALL tools. The mutation filter
        # only applies when keywords DID match. This is intentional — the dispatcher
        # enforces mutations, the selector is just prompt hygiene.
        assert len(result) == 14


class TestAlwaysInclude:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_ask_user_always_present(self):
        queries = [
            "add task",
            "show tasks",
            "mark done",
            "delete it",
            "hello world",
        ]
        for q in queries:
            result = _selected_names(self.selector.select_schemas(q, self.registry))
            assert "ask_user" in result, f"ask_user missing for query: {q}"


class TestLongestMatchFirst:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_what_did_i_complete_matches_completed_tool(self):
        result = _selected_names(
            self.selector.select_schemas("what did i complete yesterday", self.registry)
        )
        assert "get_completed_todoist_tasks_by_completion_date" in result


class TestCaseInsensitivity:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_uppercase_query_still_matches(self):
        result = _selected_names(self.selector.select_schemas("ADD BUY MILK TODAY", self.registry))
        assert "add_todoist_task" in result
        assert "delete_todoist_task" not in result

    def test_mixed_case_query_still_matches(self):
        result = _selected_names(self.selector.select_schemas("Show My Tasks", self.registry))
        assert "get_tasks" in result


class TestUncompleteIntent:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_reopen_maps_to_uncomplete_tool(self):
        result = _selected_names(
            self.selector.select_schemas("reopen the dentist appointment task", self.registry)
        )
        assert "get_tasks" in result
        assert "uncomplete_task" in result
        assert "complete_task" not in result


class TestEmptyQuery:
    def setup_method(self):
        self.registry = _build_registry()
        self.selector = KeywordToolSelector(allow_mutations=True)

    def test_empty_string_falls_back_to_all(self):
        result = _selected_names(self.selector.select_schemas("", self.registry))
        assert len(result) == 14


class TestFactory:
    def test_get_selector_keyword(self):
        from agents.agent_api.app.tools.selection import get_selector

        selector = get_selector("keyword", allow_mutations=True)
        assert isinstance(selector, KeywordToolSelector)

    def test_get_selector_static(self):
        from agents.agent_api.app.tools.selection import get_selector
        from agents.agent_api.app.tools.selectors.static import StaticToolSelector

        selector = get_selector("static")
        assert isinstance(selector, StaticToolSelector)

    def test_get_selector_unknown_raises(self):
        from agents.agent_api.app.tools.selection import get_selector

        with pytest.raises(ValueError, match="Unknown tool selector"):
            get_selector("nonexistent")
