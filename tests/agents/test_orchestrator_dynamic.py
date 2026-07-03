"""Stage 2 tests: dynamic orchestrator prompt (user name + conditional tools)."""

from agents.agent_api.app.graph.prompts.orchestrator import (
    _build_role_line,
    get_orchestrator_prompt,
    get_system_prompt,
)
from agents.agent_api.app.graph.prompts.context import build_initial_messages


class TestBuildRoleLine:
    """Tests for _build_role_line."""

    def test_default_uses_the_user(self):
        line = _build_role_line()
        assert "the user's personal assistant" in line

    def test_custom_name(self):
        line = _build_role_line("Zachary")
        assert "Zachary's personal assistant" in line
        assert "Jerry" not in line

    def test_preserves_service_descriptions(self):
        line = _build_role_line("Zachary")
        assert "Google Calendar" in line
        assert "Todoist" in line


class TestGetOrchestratorPromptDynamic:
    """Tests for get_orchestrator_prompt with user_name and calendar_enabled."""

    def test_with_user_name(self):
        prompt = get_orchestrator_prompt(user_name="Zachary")
        assert "Zachary's personal assistant" in prompt
        assert "Jerry's personal assistant" not in prompt

    def test_without_user_name_uses_static_default(self):
        prompt = get_orchestrator_prompt()
        assert "Jerry's personal assistant" in prompt

    def test_calendar_enabled_true_shows_both_tools(self):
        prompt = get_orchestrator_prompt(calendar_enabled=True)
        assert "Todoist task tools and Google Calendar tools" in prompt

    def test_calendar_enabled_false_shows_todoist_only(self):
        prompt = get_orchestrator_prompt(calendar_enabled=False)
        assert "Available tools: Todoist task tools." in prompt
        assert "Google Calendar tools" not in prompt.split("Available tools:")[-1].split("\n")[0]

    def test_runtime_context_present(self):
        prompt = get_orchestrator_prompt(tz="Asia/Singapore", user_name="Zachary")
        assert "## Runtime context" in prompt
        assert "Asia/Singapore" in prompt


class TestGetSystemPromptPassthrough:
    """Tests for get_system_prompt passing params through."""

    def test_user_name_passed_through(self):
        prompt = get_system_prompt(user_name="Zachary")
        assert "Zachary's personal assistant" in prompt

    def test_calendar_enabled_passed_through(self):
        prompt = get_system_prompt(calendar_enabled=False)
        assert "Available tools: Todoist task tools." in prompt


class TestBuildInitialMessagesThreading:
    """Tests that build_initial_messages threads user_name and calendar_enabled."""

    def test_user_name_in_system_message(self):
        messages = build_initial_messages("hello", user_name="Zachary")
        system_msg = messages[0]["content"]
        assert "Zachary's personal assistant" in system_msg

    def test_calendar_disabled_in_system_message(self):
        messages = build_initial_messages("hello", calendar_enabled=False)
        system_msg = messages[0]["content"]
        assert "Available tools: Todoist task tools." in system_msg

    def test_backward_compat_no_args(self):
        messages = build_initial_messages("hello")
        system_msg = messages[0]["content"]
        assert "Jerry's personal assistant" in system_msg
        assert "Google Calendar tools" in system_msg
