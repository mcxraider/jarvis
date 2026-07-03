"""Stage 4 tests: end-to-end multi-user verification.

Tests the full run_jarvis flow with multiple users getting:
- Different calendar routing (user A has token, user B doesn't)
- Personalized orchestrator prompts (user name + available tools)
"""

import os
from unittest.mock import patch

import pytest

from agents.agent_api.app.graph import builder as builder_module
from agents.agent_api.app.tools.calendar.auth import GOOGLE_TOKEN_MAP_ENV
from tests.agents.test_jarvis import FakeDeepSeekAgentClient, FakeTodoistClient


_CALENDAR_TOOLS = {
    "list_calendar_events",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "get_freebusy",
}


class TestMultiUserCalendarRouting:
    """Full-flow test: different users get different calendar access."""

    def test_user_with_token_gets_calendar_tools(self, tmp_path, monkeypatch):
        """Jerry (701122767) has a token file → calendar tools exposed."""
        token_file = tmp_path / "jerry_token.json"
        token_file.write_text('{"dummy": true}')

        env = {
            GOOGLE_TOKEN_MAP_ENV: f"701122767:{token_file}",
            "GOOGLE_CALENDAR_ENABLED": "true",
        }
        monkeypatch.setattr(builder_module, "get_token_path_for_user", lambda uid: str(token_file) if uid == 701122767 else "nonexistent.json")
        monkeypatch.setattr(builder_module, "is_calendar_configured_for_user", lambda uid=None: uid == 701122767)

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi Jerry."}])

        builder_module.run_jarvis(
            user_prompt="what's on my calendar?",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=builder_module.NULL_TRACE,
            telegram_user_id=701122767,
            telegram_first_name="Jerry",
        )

        exposed = {call["function"]["name"] for call in agent_client.tool_calls[0]}
        assert _CALENDAR_TOOLS <= exposed

    def test_user_without_token_gets_no_calendar_tools(self, monkeypatch):
        """Zachary (387244560) has no token → calendar tools NOT exposed."""
        monkeypatch.setattr(builder_module, "is_calendar_configured_for_user", lambda uid=None: False)

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi Zachary."}])

        builder_module.run_jarvis(
            user_prompt="show my tasks",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=builder_module.NULL_TRACE,
            telegram_user_id=387244560,
            telegram_first_name="Zachary",
        )

        exposed = {call["function"]["name"] for call in agent_client.tool_calls[0]}
        assert exposed.isdisjoint(_CALENDAR_TOOLS)


class TestMultiUserPromptPersonalization:
    """Full-flow test: different users get personalized prompts."""

    def test_jerry_gets_personalized_prompt(self, monkeypatch):
        """Jerry's prompt says 'Jerry's personal assistant'."""
        monkeypatch.setattr(builder_module, "is_calendar_configured_for_user", lambda uid=None: True)

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}])

        builder_module.run_jarvis(
            user_prompt="hello",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=builder_module.NULL_TRACE,
            telegram_user_id=701122767,
            telegram_first_name="Jerry",
        )

        system_prompt = agent_client.calls[0][0]["content"]
        assert "Jerry's personal assistant" in system_prompt

    def test_zachary_gets_personalized_prompt(self, monkeypatch):
        """Zachary's prompt says 'Zachary's personal assistant'."""
        monkeypatch.setattr(builder_module, "is_calendar_configured_for_user", lambda uid=None: False)

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}])

        builder_module.run_jarvis(
            user_prompt="hello",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=builder_module.NULL_TRACE,
            telegram_user_id=387244560,
            telegram_first_name="Zachary",
        )

        system_prompt = agent_client.calls[0][0]["content"]
        assert "Zachary's personal assistant" in system_prompt
        assert "Jerry" not in system_prompt

    def test_user_without_calendar_sees_todoist_only_in_prompt(self, monkeypatch):
        """When calendar is not configured, prompt only mentions Todoist tools."""
        monkeypatch.setattr(builder_module, "is_calendar_configured_for_user", lambda uid=None: False)

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}])

        builder_module.run_jarvis(
            user_prompt="hello",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=builder_module.NULL_TRACE,
            telegram_user_id=387244560,
            telegram_first_name="Zachary",
        )

        system_prompt = agent_client.calls[0][0]["content"]
        assert "Available tools: Todoist task tools." in system_prompt

    def test_user_with_calendar_sees_both_tools_in_prompt(self, monkeypatch):
        """When calendar IS configured, prompt mentions both services."""
        monkeypatch.setattr(builder_module, "is_calendar_configured_for_user", lambda uid=None: True)

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}])

        builder_module.run_jarvis(
            user_prompt="hello",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=builder_module.NULL_TRACE,
            telegram_user_id=701122767,
            telegram_first_name="Jerry",
        )

        system_prompt = agent_client.calls[0][0]["content"]
        assert "Todoist task tools and Google Calendar tools" in system_prompt


class TestNoTelegramFirstName:
    """Backward compat: when no first name is provided, falls back gracefully."""

    def test_no_first_name_uses_default(self, monkeypatch):
        monkeypatch.setattr(builder_module, "is_calendar_configured_for_user", lambda uid=None: True)

        agent_client = FakeDeepSeekAgentClient([{"role": "assistant", "content": "Hi."}])

        builder_module.run_jarvis(
            user_prompt="hello",
            agent_client=agent_client,
            todoist_client=FakeTodoistClient(),
            tracer=builder_module.NULL_TRACE,
            telegram_user_id=701122767,
            telegram_first_name=None,
        )

        system_prompt = agent_client.calls[0][0]["content"]
        # Falls back to static ORCHESTRATOR_PROMPT which says "Jerry's"
        assert "Jerry's personal assistant" in system_prompt
