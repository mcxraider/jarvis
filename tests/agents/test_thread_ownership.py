"""Tests for thread ownership validation."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from agents.agent_api.app.api.thread_ownership import validate_thread_ownership
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


class TestOwnershipNoop:
    def test_noop_when_dsn_is_falsy(self):
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            validate_thread_ownership("t-1", IDENTITY)

    def test_noop_when_telegram_user_id_is_none(self):
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ):
            validate_thread_ownership("t-1", None)


class TestOwnershipAllow:
    def test_allows_when_no_row_found(self):
        cursor = FakeCursor(row=None)
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            validate_thread_ownership("legacy-thread", IDENTITY)

    def test_allows_when_owner_matches(self):
        cursor = FakeCursor(row=(True,))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            validate_thread_ownership("t-1", IDENTITY)


class TestOwnershipReject:
    def test_raises_403_when_owner_differs(self):
        cursor = FakeCursor(row=(False,))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with pytest.raises(HTTPException) as exc_info:
                validate_thread_ownership("t-1", IDENTITY)
            assert exc_info.value.status_code == 403


class TestOwnershipFailOpen:
    def test_allows_on_generic_exception(self, caplog):
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("db down"),
        ):
            with caplog.at_level(logging.WARNING):
                validate_thread_ownership("t-1", IDENTITY)
        assert "Thread ownership check failed" in caplog.text

    def test_http_exception_is_not_swallowed(self):
        cursor = FakeCursor(row=(False,))
        pool = FakePool(cursor)
        with patch(
            "agents.agent_api.app.middleware.thread_ownership.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with pytest.raises(HTTPException) as exc_info:
                validate_thread_ownership("t-1", IDENTITY)
            assert exc_info.value.status_code == 403
