"""Tests for per-user fresh-thread quota limiting."""

from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from agents.agent_api.app.middleware.rate_limit import consume_new_thread_quota
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


def reset_at():
    return datetime.now(timezone.utc) + timedelta(hours=4)


class TestThreadQuotaNoop:
    def test_noop_when_dsn_is_falsy(self):
        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            assert consume_new_thread_quota(IDENTITY) is None

    def test_noop_when_identity_is_none(self):
        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ):
            assert consume_new_thread_quota(None) is None


class TestThreadQuotaAllow:
    @pytest.mark.parametrize("used", [1, 50, 100])
    def test_allows_allowed_rows(self, used):
        cursor = FakeCursor(row=(True, used, 100, reset_at()))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            result = consume_new_thread_quota(IDENTITY)

        assert result is not None
        assert result.allowed is True
        assert result.threads_used == used
        assert result.thread_limit == 100


class TestThreadQuotaReject:
    def test_raises_429_when_denied(self):
        cursor = FakeCursor(row=(False, 100, 100, reset_at()))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with pytest.raises(HTTPException) as exc_info:
                consume_new_thread_quota(IDENTITY)

        assert exc_info.value.status_code == 429
        assert int(exc_info.value.headers["Retry-After"]) > 0
        assert "Daily thread limit reached" in exc_info.value.detail
        assert "SGT" in exc_info.value.detail or "+08" in exc_info.value.detail


class TestThreadQuotaSql:
    def test_calls_try_consume_thread_quota_with_telegram_id(self):
        cursor = FakeCursor(row=(True, 1, 100, reset_at()))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            consume_new_thread_quota(TelegramIdentity(telegram_id=12345))

        assert len(cursor.statements) == 1
        sql, params = cursor.statements[0]
        assert "public.try_consume_thread_quota(%s)" in sql
        assert params == (12345,)


class TestThreadQuotaFailurePolicy:
    def test_fails_open_on_pool_timeout(self, caplog):
        from psycopg_pool import PoolTimeout

        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=PoolTimeout("pool busy"),
        ):
            with caplog.at_level(logging.WARNING):
                assert consume_new_thread_quota(IDENTITY) is None
        assert "failed open" in caplog.text

    def test_fails_open_on_operational_error(self, caplog):
        from psycopg import OperationalError

        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=OperationalError("connection unavailable"),
        ):
            with caplog.at_level(logging.WARNING):
                assert consume_new_thread_quota(IDENTITY) is None
        assert "failed open" in caplog.text

    def test_fails_closed_on_known_quota_sql_error(self):
        from psycopg import errors

        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=errors.UndefinedFunction("missing quota function"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                consume_new_thread_quota(IDENTITY)

        assert exc_info.value.status_code == 503

    def test_http_exception_is_not_swallowed(self):
        cursor = FakeCursor(row=(False, 100, 100, reset_at()))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.rate_limit.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with pytest.raises(HTTPException) as exc_info:
                consume_new_thread_quota(IDENTITY)
        assert exc_info.value.status_code == 429
