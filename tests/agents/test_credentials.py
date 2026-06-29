"""Tests for credential lookup (agents.agent_api.app.credentials)."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.agent_api.app.credentials import get_credential


class FakeCursor:
    def __init__(self, row=None):
        self.statements = []
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, params))

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


def _make_pool(row=None):
    cursor = FakeCursor(row=row)
    pool = FakePool(cursor)
    return pool, cursor


class TestGetCredentialShortCircuit:
    def test_returns_none_when_telegram_user_id_is_none(self):
        result = get_credential(None, service="todoist")
        assert result is None

    def test_returns_none_when_postgres_dsn_is_falsy(self):
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            result = get_credential(12345, service="todoist")
        assert result is None

    def test_does_not_touch_pool_when_short_circuited(self):
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn=""),
        ), patch("agents.agent_api.app.db.get_pool") as mock_pool:
            get_credential(None)
            get_credential(12345)
        mock_pool.assert_not_called()


class TestGetCredentialSuccess:
    def test_returns_api_key_when_row_found(self):
        pool, _cursor = _make_pool(row=("my-todoist-key",))
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            result = get_credential(99, service="todoist")
        assert result == "my-todoist-key"

    def test_returns_none_when_no_row(self):
        pool, _cursor = _make_pool(row=None)
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            result = get_credential(99, service="todoist")
        assert result is None

    def test_sql_filters_on_service_and_is_active(self):
        pool, cursor = _make_pool(row=("key",))
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            get_credential(42, service="notion")

        assert len(cursor.statements) == 1
        sql, params = cursor.statements[0]
        assert "uc.service = %s" in sql
        assert "uc.is_active = TRUE" in sql
        assert params == ("42", "notion")


class TestGetCredentialFailOpen:
    def test_returns_none_on_pool_exception(self, caplog):
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = get_credential(99, service="todoist")

        assert result is None
        assert "Credential lookup failed" in caplog.text


class TestTodoistApiKeyPriority:
    """Integration test for todoist_api_key_for_telegram_user priority order."""

    def test_db_key_wins(self):
        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            return_value=FakePool(FakeCursor(row=("db-key",))),
        ):
            from agents.agent_api.app.tools.todoist.client import (
                todoist_api_key_for_telegram_user,
            )

            result = todoist_api_key_for_telegram_user(123)
        assert result == "db-key"

    def test_falls_through_to_token_map(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_KEYS_BY_TELEGRAM_USER_ID", "123:map-key")
        monkeypatch.delenv("TODOIST_API_KEY", raising=False)

        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            from agents.agent_api.app.tools.todoist.client import (
                todoist_api_key_for_telegram_user,
            )

            result = todoist_api_key_for_telegram_user(123)
        assert result == "map-key"

    def test_falls_through_to_global_env_when_no_map(self, monkeypatch):
        monkeypatch.delenv("TODOIST_API_KEYS_BY_TELEGRAM_USER_ID", raising=False)
        monkeypatch.setenv("TODOIST_API_KEY", "global-key")

        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            from agents.agent_api.app.tools.todoist.client import (
                todoist_api_key_for_telegram_user,
            )

            result = todoist_api_key_for_telegram_user(123)
        assert result == "global-key"

    def test_map_configured_but_user_missing_returns_none(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_KEYS_BY_TELEGRAM_USER_ID", "111:token-one")
        monkeypatch.setenv("TODOIST_API_KEY", "global-key")

        with patch(
            "agents.agent_api.app.credentials.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            from agents.agent_api.app.tools.todoist.client import (
                todoist_api_key_for_telegram_user,
            )

            result = todoist_api_key_for_telegram_user(222)
        assert result is None
