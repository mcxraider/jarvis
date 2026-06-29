"""Tests for the shared DB connection pool (agents.agent_api.app.db)."""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agents.agent_api.app.db as db_module


class FakePool:
    """Minimal fake matching psycopg_pool.ConnectionPool interface."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.opened = False
        self.waited = False
        self.closed = False

    def open(self):
        self.opened = True

    def wait(self, **kwargs):
        self.waited = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    """Reset the module-level singleton before each test."""
    monkeypatch.setattr(db_module, "_pool", None)


class TestGetPoolRaises:
    def test_raises_when_dsn_is_empty(self):
        with pytest.raises(RuntimeError, match="Shared DB pool requires"):
            db_module.get_pool()

    def test_raises_when_dsn_is_none(self):
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn=None),
        ):
            with pytest.raises(RuntimeError, match="Shared DB pool requires"):
                db_module.get_pool()


class TestGetPoolSingleton:
    def test_returns_same_object_on_repeated_calls(self):
        fake_pool = FakePool()
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("psycopg_pool.ConnectionPool", return_value=fake_pool) as mock_ctor:
            first = db_module.get_pool()
            second = db_module.get_pool()

        assert first is second
        assert first is fake_pool
        assert mock_ctor.call_count == 1

    def test_pool_opened_and_waited(self):
        fake_pool = FakePool()
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("psycopg_pool.ConnectionPool", return_value=fake_pool):
            db_module.get_pool()

        assert fake_pool.opened is True
        assert fake_pool.waited is True

    def test_pool_constructor_receives_expected_kwargs(self):
        fake_pool = FakePool()
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("psycopg_pool.ConnectionPool", return_value=fake_pool) as mock_ctor:
            db_module.get_pool()

        mock_ctor.assert_called_once_with(
            conninfo="postgresql://fake",
            min_size=2,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=False,
        )


class FailingWaitPool(FakePool):
    """Pool that raises on wait() to simulate connection timeout."""

    def wait(self, **kwargs):
        raise RuntimeError("timeout")


class TestPoolCreationFailure:
    def test_wait_failure_cleans_up_and_leaves_pool_none(self):
        fake_pool = FailingWaitPool()
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("psycopg_pool.ConnectionPool", return_value=fake_pool):
            with pytest.raises(RuntimeError, match="timeout"):
                db_module.get_pool()

        assert db_module._pool is None
        assert fake_pool.closed is True

    def test_retry_succeeds_after_prior_failure(self):
        bad_pool = FailingWaitPool()
        good_pool = FakePool()

        call_count = [0]

        def pool_factory(**kwargs):
            call_count[0] += 1
            return bad_pool if call_count[0] == 1 else good_pool

        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch("psycopg_pool.ConnectionPool", side_effect=pool_factory):
            with pytest.raises(RuntimeError):
                db_module.get_pool()
            result = db_module.get_pool()

        assert result is good_pool


class FailingClosePool(FakePool):
    """Pool that raises on close()."""

    def close(self):
        raise RuntimeError("close failed")


class TestClosePool:
    def test_closes_pool_and_resets_singleton(self):
        fake_pool = FakePool()
        db_module._pool = fake_pool

        db_module.close_pool()

        assert db_module._pool is None
        assert fake_pool.closed is True

    def test_noop_when_pool_not_opened(self):
        db_module._pool = None
        db_module.close_pool()
        assert db_module._pool is None

    def test_exception_during_close_still_resets_pool(self):
        fake_pool = FailingClosePool()
        db_module._pool = fake_pool

        db_module.close_pool()

        assert db_module._pool is None


class TestImportSafety:
    def test_import_succeeds_without_dsn(self):
        mod = importlib.import_module("agents.agent_api.app.db")
        assert hasattr(mod, "get_pool")

    def test_close_pool_is_importable(self):
        mod = importlib.import_module("agents.agent_api.app.db")
        assert hasattr(mod, "close_pool")
