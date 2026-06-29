"""Tests for usage logging (_log_usage in builder.py)."""

import logging
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from agents.agent_api.app.graph.builder import _log_usage


@dataclass
class FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


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


class TestLogUsageNoop:
    def test_noop_when_telegram_user_id_is_none(self):
        with patch("agents.agent_api.app.db.get_pool") as mock_pool:
            _log_usage(None, "t-1", FakeUsage(total_tokens=100), 500, "deepseek")
        mock_pool.assert_not_called()

    def test_noop_when_total_tokens_is_zero(self):
        with patch("agents.agent_api.app.db.get_pool") as mock_pool:
            _log_usage(42, "t-1", FakeUsage(total_tokens=0), 500, "deepseek")
        mock_pool.assert_not_called()

    def test_noop_when_total_tokens_is_falsy(self):
        usage = FakeUsage()
        usage.total_tokens = None  # type: ignore[assignment]
        with patch("agents.agent_api.app.db.get_pool") as mock_pool:
            _log_usage(42, "t-1", usage, 500, "deepseek")
        mock_pool.assert_not_called()


class TestLogUsageInsert:
    def test_issues_insert_with_expected_params(self):
        pool = FakePool()
        usage = FakeUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _log_usage(42, "thread-abc", usage, 1234, "deepseek-v4-flash")

        assert len(pool.cursor_instance.statements) == 1
        sql, params = pool.cursor_instance.statements[0]
        assert "INSERT INTO usage_logs" in sql
        assert "event_type" in sql
        assert params == (
            "thread-abc",
            "deepseek-v4-flash",
            100,
            50,
            1234,
            "42",
        )

    def test_defaults_none_token_values_to_zero(self):
        pool = FakePool()
        usage = FakeUsage(total_tokens=10)
        usage.prompt_tokens = None  # type: ignore[assignment]
        usage.completion_tokens = None  # type: ignore[assignment]
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _log_usage(99, "t-1", usage, 200, "model-x")

        _sql, params = pool.cursor_instance.statements[0]
        assert params[2] == 0  # prompt_tokens
        assert params[3] == 0  # completion_tokens


class TestLogUsageFireAndForget:
    def test_pool_exception_is_swallowed(self, caplog):
        with patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("db down"),
        ):
            with caplog.at_level(logging.WARNING):
                _log_usage(42, "t-1", FakeUsage(total_tokens=100), 500, "model")

        assert "Usage logging failed" in caplog.text

    def test_cursor_exception_is_swallowed(self, caplog):
        pool = FakePool()
        pool.cursor_instance.execute = lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("bad sql")
        )
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with caplog.at_level(logging.WARNING):
                _log_usage(42, "t-1", FakeUsage(total_tokens=100), 500, "model")

        assert "Usage logging failed" in caplog.text
