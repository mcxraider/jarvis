"""Tests for user preferences lookup and timezone threading."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.agent_api.app.credentials import get_user_preferences
from agents.agent_api.app.graph.prompts.orchestrator import (
    _user_timezone,
    get_orchestrator_prompt,
    get_system_prompt,
)


class FakeCursor:
    def __init__(self, row=None):
        self.statements = []
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((" ".join(statement.split()), params))

    def fetchone(self):
        return self._row


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class FakePool:
    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self):
        return FakeConnection(self._cursor)


class TestGetUserPreferences:
    def test_returns_empty_dict_when_telegram_user_id_is_none(self):
        assert get_user_preferences(None) == {}

    def test_returns_empty_dict_when_dsn_is_falsy(self):
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            assert get_user_preferences(42) == {}

    def test_returns_preferences_dict_from_row(self):
        prefs = {"timezone": "America/New_York", "language": "en"}
        cursor = FakeCursor(row=(prefs,))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            result = get_user_preferences(42)
        assert result == prefs

    def test_returns_empty_dict_when_no_row(self):
        cursor = FakeCursor(row=None)
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            assert get_user_preferences(42) == {}

    def test_returns_empty_dict_when_row_value_is_not_dict(self):
        cursor = FakeCursor(row=("not a dict",))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            assert get_user_preferences(42) == {}

    def test_returns_empty_dict_on_exception(self, caplog):
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level(logging.WARNING):
                result = get_user_preferences(42)
        assert result == {}
        assert "Preferences lookup failed" in caplog.text


class TestUserTimezone:
    def test_override_wins(self):
        assert _user_timezone("Europe/London") == "Europe/London"

    def test_env_var_used_when_no_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_USER_TIMEZONE", "Asia/Tokyo")
        assert _user_timezone(None) == "Asia/Tokyo"

    def test_falls_back_to_utc_on_detection_failure(self, monkeypatch):
        monkeypatch.delenv("JARVIS_USER_TIMEZONE", raising=False)
        with patch(
            "agents.agent_api.app.graph.prompts.orchestrator.datetime"
        ) as mock_dt:
            mock_dt.now.side_effect = Exception("no tz")
            result = _user_timezone(None)
        assert result == "UTC"


class TestPromptTimezoneThreading:
    def test_get_orchestrator_prompt_embeds_timezone(self):
        prompt = get_orchestrator_prompt("America/Chicago")
        assert "User timezone: America/Chicago" in prompt

    def test_get_system_prompt_threads_timezone(self):
        prompt = get_system_prompt("Asia/Taipei")
        assert "User timezone: Asia/Taipei" in prompt

    def test_build_initial_messages_threads_timezone(self):
        from agents.agent_api.app.graph.prompts.context import build_initial_messages

        messages = build_initial_messages("hello", timezone="Europe/Berlin")
        system_content = messages[0]["content"]
        assert "User timezone: Europe/Berlin" in system_content

    def test_build_initial_state_threads_timezone(self):
        from agents.agent_api.app.graph.builder import build_initial_state

        state = build_initial_state("test prompt", timezone="US/Pacific")
        system_content = state["messages"][0]["content"]
        assert "User timezone: US/Pacific" in system_content
