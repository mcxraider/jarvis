"""Tests for thread registration (_register_thread in builder.py)."""

import logging
from unittest.mock import patch

import pytest

from agents.agent_api.app.graph.builder import _register_thread
from agents.agent_api.app.user_context.identity import TelegramIdentity

IDENTITY = TelegramIdentity(telegram_id=42)


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


class TestRegisterThreadNoop:
    def test_noop_when_telegram_user_id_is_none(self):
        with patch("agents.agent_api.app.db.get_pool") as mock_pool:
            _register_thread("thread-1", None, "hello", "completed", False)
        mock_pool.assert_not_called()


class TestRegisterThreadInsert:
    def test_non_resume_issues_insert_on_conflict(self):
        pool = FakePool()
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _register_thread("t-123", IDENTITY, "create a task", "completed", False)

        assert len(pool.cursor_instance.statements) == 1
        sql, params = pool.cursor_instance.statements[0]
        assert "INSERT INTO threads" in sql
        assert "ON CONFLICT (thread_id) DO UPDATE" in sql
        assert params == ("t-123", 42, "create a task", "completed")

    def test_non_resume_passes_user_prompt_for_title(self):
        pool = FakePool()
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _register_thread("t-1", IDENTITY, "a very long prompt", "interrupted", False)

        sql, params = pool.cursor_instance.statements[0]
        assert "LEFT(%s, 100)" in sql
        assert params[2] == "a very long prompt"


class TestRegisterThreadResume:
    def test_resume_issues_update(self):
        pool = FakePool()
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _register_thread("t-456", IDENTITY, "yes please", "completed", True)

        assert len(pool.cursor_instance.statements) == 1
        sql, params = pool.cursor_instance.statements[0]
        assert "UPDATE threads" in sql
        assert "message_count = message_count + 1" in sql
        assert "INSERT" not in sql
        assert params == ("completed", "t-456")


class TestRegisterThreadFireAndForget:
    def test_pool_exception_is_swallowed(self, caplog):
        with patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("no connection"),
        ):
            with caplog.at_level(logging.WARNING):
                _register_thread("t-err", IDENTITY, "test", "completed", False)

        assert "Thread registration failed" in caplog.text

    def test_cursor_exception_is_swallowed(self, caplog):
        pool = FakePool()
        pool.cursor_instance.execute = lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("bad sql")
        )
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with caplog.at_level(logging.WARNING):
                _register_thread("t-err2", IDENTITY, "test", "completed", False)

        assert "Thread registration failed" in caplog.text
