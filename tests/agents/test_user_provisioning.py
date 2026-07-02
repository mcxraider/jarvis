"""Tests for automatic users-row provisioning."""

import logging
from unittest.mock import patch

from agents.agent_api.app.graph.builder import _ensure_user


class FakeCursor:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, params))


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
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def connection(self):
        return FakeConnection(self.cursor_instance)


class TestEnsureUser:
    def test_noop_when_telegram_user_id_is_none(self):
        with patch("agents.agent_api.app.db.get_pool") as mock_pool:
            _ensure_user(None, "tester", "Test")

        mock_pool.assert_not_called()

    def test_inserts_user_and_ignores_existing_row(self):
        pool = FakePool()
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _ensure_user(42, "tester", "Test")

        assert len(pool.cursor_instance.statements) == 1
        sql, params = pool.cursor_instance.statements[0]
        assert "INSERT INTO users" in sql
        assert "ON CONFLICT (telegram_user_id) DO NOTHING" in sql
        assert params == ("42", "tester", "Test")

    def test_failure_is_swallowed_and_logged(self, caplog):
        with patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("db down"),
        ):
            with caplog.at_level(logging.WARNING):
                _ensure_user(42, "tester", "Test")

        assert "User provisioning failed" in caplog.text
