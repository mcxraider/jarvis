"""Tests for the Vault secret building block (user_context.secrets)."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

from agents.agent_api.app.user_context.secrets import resolve_connection_secret


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


def _make_pool(row=None):
    cursor = FakeCursor(row=row)
    return FakePool(cursor), cursor


class TestShortCircuit:
    def test_returns_none_when_telegram_user_id_is_none(self):
        assert resolve_connection_secret(None, "todoist") is None

    def test_returns_none_when_postgres_dsn_is_falsy(self):
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn=""),
        ):
            assert resolve_connection_secret(12345, "todoist") is None

    def test_does_not_touch_pool_when_short_circuited(self):
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn=""),
        ), patch("agents.agent_api.app.db.get_pool") as mock_pool:
            resolve_connection_secret(None, "todoist")
            resolve_connection_secret(12345, "todoist")
        mock_pool.assert_not_called()


class TestSuccess:
    def test_returns_credential_when_row_found(self):
        pool, _cursor = _make_pool(row=("connection-id", "my-todoist-key"))
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            credential = resolve_connection_secret(99, "todoist")
        assert credential is not None
        assert credential.secret == "my-todoist-key"
        assert credential.connection_id == "connection-id"
        assert credential.provider == "todoist"

    def test_returns_none_when_no_row(self):
        pool, _cursor = _make_pool(row=None)
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            assert resolve_connection_secret(99, "todoist") is None

    def test_returns_none_when_secret_is_null(self):
        pool, _cursor = _make_pool(row=("connection-id", None))
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            assert resolve_connection_secret(99, "todoist") is None

    def test_sql_filters_on_provider_status_and_enabled(self):
        pool, cursor = _make_pool(row=("connection-id", "key"))
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            resolve_connection_secret(42, "notion")

        assert len(cursor.statements) == 1
        sql, params = cursor.statements[0]
        assert "connection.provider = %s" in sql
        assert "connection.status = 'connected'" in sql
        assert "connection.is_enabled" in sql
        assert params == ("42", "notion")


class TestFailSoft:
    def test_returns_none_on_pool_exception(self, caplog):
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = resolve_connection_secret(99, "todoist")

        assert result is None
        assert "Integration credential lookup failed" in caplog.text


_SENTINEL_SECRET = "SENTINEL-SECRET-DO-NOT-LOG"


class TestSecretNeverLogged:
    """The resolved secret value must never reach the logs (#5)."""

    def test_secret_absent_from_logs_on_success(self, caplog):
        pool, _cursor = _make_pool(row=("connection-id", _SENTINEL_SECRET))
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("agents.agent_api.app.db.get_pool", return_value=pool):
            with caplog.at_level(logging.DEBUG):
                credential = resolve_connection_secret(99, "todoist")

        assert credential is not None and credential.secret == _SENTINEL_SECRET
        assert _SENTINEL_SECRET not in caplog.text

    def test_secret_absent_from_logs_on_failure(self, caplog):
        # Even when resolution fails, the (never-resolved) secret cannot leak, and the
        # warning path logs only the exception type.
        with patch(
            "agents.agent_api.app.user_context.secrets.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "agents.agent_api.app.db.get_pool",
            side_effect=RuntimeError(_SENTINEL_SECRET),  # secret-shaped error text
        ):
            with caplog.at_level(logging.DEBUG):
                result = resolve_connection_secret(99, "todoist")

        assert result is None
        assert _SENTINEL_SECRET not in caplog.text


class TestHealthProbeDoesNotLogToken:
    """The /status Todoist probe resolves a token but must not log its value (#5)."""

    def test_probe_never_logs_the_token(self, caplog):
        import urllib.request

        from agents.agent_api.app.api.routes import health

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"results": []}'

        with patch(
            "agents.agent_api.app.user_context.secrets.resolve_connection_secret",
            return_value=SimpleNamespace(secret=_SENTINEL_SECRET),
        ), patch.object(urllib.request, "urlopen", return_value=_FakeResponse()):
            with caplog.at_level(logging.DEBUG):
                result = health._check_todoist(telegram_user_id=99)

        assert result["ok"] is True
        assert _SENTINEL_SECRET not in caplog.text
        assert _SENTINEL_SECRET not in result["detail"]
