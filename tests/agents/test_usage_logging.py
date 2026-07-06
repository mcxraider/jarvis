"""Tests for usage logging (_log_usage in builder.py)."""

import logging
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import patch

import pytest

from agents.agent_api.app.graph.builder import _log_usage
from agents.agent_api.app.user_context.identity import TelegramIdentity

IDENTITY = TelegramIdentity(telegram_id=42)


@dataclass
class FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


class FakeCursor:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        parameter_count = len(params) if params is not None else 0
        placeholder_count = normalized.count("%s")
        if placeholder_count != parameter_count:
            raise ValueError(
                f"SQL has {placeholder_count} placeholders but {parameter_count} parameters"
            )
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

    def test_noop_when_total_tokens_is_zero(self, caplog):
        with patch("agents.agent_api.app.db.get_pool") as mock_pool:
            with caplog.at_level(logging.WARNING):
                _log_usage(IDENTITY, "t-1", FakeUsage(total_tokens=0), 500, "deepseek")
        mock_pool.assert_not_called()
        assert "token usage is absent" in caplog.text

    def test_noop_when_total_tokens_is_falsy(self, caplog):
        usage = FakeUsage()
        usage.total_tokens = None  # type: ignore[assignment]
        with patch("agents.agent_api.app.db.get_pool") as mock_pool:
            with caplog.at_level(logging.WARNING):
                _log_usage(IDENTITY, "t-1", usage, 500, "deepseek")
        mock_pool.assert_not_called()
        assert "token usage is absent" in caplog.text


class TestLogUsageInsert:
    def test_issues_insert_with_expected_params(self):
        pool = FakePool()
        usage = FakeUsage(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cached_tokens=40,
        )
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _log_usage(IDENTITY, "thread-abc", usage, 1234, "deepseek-v4-flash")

        assert len(pool.cursor_instance.statements) == 1
        sql, params = pool.cursor_instance.statements[0]
        assert "INSERT INTO usage_logs" in sql
        assert "event_type" in sql
        assert params == (
            42,
            "thread-abc",
            "deepseek-v4-flash",
            100,
            40,
            60,
            50,
            Decimal("0.0000"),
            1234,
        )
        assert sql.count("%s") == len(params)

    def test_defaults_none_token_values_to_zero(self):
        pool = FakePool()
        usage = FakeUsage(total_tokens=10)
        usage.prompt_tokens = None  # type: ignore[assignment]
        usage.completion_tokens = None  # type: ignore[assignment]
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            _log_usage(IDENTITY, "t-1", usage, 200, "model-x")

        _sql, params = pool.cursor_instance.statements[0]
        assert params[3] == 0  # prompt_tokens
        assert params[4] == 0  # cached_input_tokens
        assert params[5] == 0  # uncached_input_tokens
        assert params[6] == 0  # output_tokens
        assert params[7] is None  # unknown model

    def test_invalid_cache_metadata_records_null_breakdown_and_cost(self, caplog):
        pool = FakePool()
        usage = FakeUsage(
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
            cached_tokens=11,
        )

        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with caplog.at_level(logging.WARNING):
                _log_usage(IDENTITY, "t-1", usage, 200, "deepseek-v4-flash")

        _sql, params = pool.cursor_instance.statements[0]
        assert params[4] is None
        assert params[5] is None
        assert params[7] is None
        assert "invalid token metadata" in caplog.text


class TestLogUsageFireAndForget:
    def test_pool_exception_is_swallowed(self, caplog):
        with patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("db down"),
        ):
            with caplog.at_level(logging.WARNING):
                _log_usage(IDENTITY, "t-1", FakeUsage(total_tokens=100), 500, "model")

        assert "Usage logging failed" in caplog.text
        assert caplog.records[-1].error_message == "db down"

    def test_cursor_exception_is_swallowed(self, caplog):
        pool = FakePool()
        pool.cursor_instance.execute = lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("bad sql")
        )
        with patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with caplog.at_level(logging.WARNING):
                _log_usage(IDENTITY, "t-1", FakeUsage(total_tokens=100), 500, "model")

        assert "Usage logging failed" in caplog.text
        assert caplog.records[-1].error_message == "bad sql"
