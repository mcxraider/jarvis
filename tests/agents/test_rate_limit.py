"""Tests for per-user rate limiting."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from agents.agent_api.app.api.rate_limit import check_rate_limit
from agents.agent_api.app.user_context.identity import TelegramIdentity

IDENTITY = TelegramIdentity(telegram_id=42)


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


class TestRateLimitNoop:
    def test_noop_when_dsn_is_falsy(self):
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            check_rate_limit(IDENTITY)

    def test_noop_when_telegram_user_id_is_none(self):
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ):
            check_rate_limit(None)


class TestRateLimitAllow:
    def test_allows_when_no_row(self):
        cursor = FakeCursor(row=None)
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            check_rate_limit(IDENTITY)

    def test_allows_when_under_limit(self):
        cursor = FakeCursor(row=(5, 100))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            check_rate_limit(IDENTITY)

    def test_allows_when_at_limit(self):
        cursor = FakeCursor(row=(100, 100))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            check_rate_limit(IDENTITY)


class TestRateLimitReject:
    def test_raises_429_when_over_limit(self):
        cursor = FakeCursor(row=(101, 100))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with pytest.raises(HTTPException) as exc_info:
                check_rate_limit(IDENTITY)
            assert exc_info.value.status_code == 429
            assert exc_info.value.headers["Retry-After"] == "3600"


class TestRateLimitSql:
    def test_passes_telegram_user_id_as_string(self):
        cursor = FakeCursor(row=(1, 100))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            check_rate_limit(TelegramIdentity(telegram_id=12345))

        assert len(cursor.statements) == 1
        _sql, params = cursor.statements[0]
        assert params == (12345,)

    def test_sql_contains_reset_logic(self):
        cursor = FakeCursor(row=(1, 100))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            check_rate_limit(IDENTITY)

        sql, _params = cursor.statements[0]
        assert "WHEN rl.reset_at <= NOW() THEN 1" in sql
        assert "RETURNING" in sql

    def test_sql_contains_least_cap(self):
        cursor = FakeCursor(row=(1, 100))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            check_rate_limit(IDENTITY)

        sql, _params = cursor.statements[0]
        assert "LEAST" in sql
        assert "daily_request_limit + 1" in sql


class TestRateLimitFailOpen:
    def test_allows_on_generic_exception(self, caplog):
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("pool dead"),
        ):
            with caplog.at_level(logging.WARNING):
                check_rate_limit(IDENTITY)
        assert "Rate limit check failed" in caplog.text

    def test_http_exception_is_not_swallowed(self):
        cursor = FakeCursor(row=(200, 100))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.api.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with pytest.raises(HTTPException) as exc_info:
                check_rate_limit(IDENTITY)
            assert exc_info.value.status_code == 429
