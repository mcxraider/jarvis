"""Tests for the shared DB connection pool (agents.agent_api.app.db)."""

import asyncio
import importlib
import threading
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
    monkeypatch.setattr(db_module, "_async_pool", None)
    monkeypatch.setattr(db_module, "_async_pool_loop", None)
    monkeypatch.setattr(db_module, "_async_pool_open_task", None)


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


class AsyncFakePool:
    """Minimal fake matching psycopg_pool.AsyncConnectionPool."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.opened = False
        self.wait_timeout = None
        self.closed = False

    async def open(self):
        self.opened = True

    async def wait(self, *, timeout):
        self.wait_timeout = timeout

    async def close(self):
        self.closed = True


class FailingAsyncWaitPool(AsyncFakePool):
    async def wait(self, *, timeout):
        raise RuntimeError("async timeout")


class FailingAsyncClosePool(AsyncFakePool):
    async def close(self):
        raise RuntimeError("async close failed")


class TestAsyncPool:
    def test_no_dsn_skips_pool_creation(self):
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn=None),
        ), patch("psycopg_pool.AsyncConnectionPool") as constructor:
            result = asyncio.run(db_module.open_async_pool())

        assert result is None
        constructor.assert_not_called()

    def test_open_reuses_ready_pool_and_uses_expected_configuration(self):
        fake_pool = AsyncFakePool()
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "psycopg_pool.AsyncConnectionPool",
            return_value=fake_pool,
        ) as constructor:
            async def open_twice():
                first = await db_module.open_async_pool()
                second = await db_module.open_async_pool()
                assert db_module.get_async_pool() is fake_pool
                await db_module.close_async_pool()
                return first, second

            first, second = asyncio.run(open_twice())

        assert first is second is fake_pool
        assert fake_pool.opened is True
        assert fake_pool.wait_timeout == 5.0
        constructor.assert_called_once_with(
            conninfo="postgresql://fake",
            min_size=2,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=False,
        )

    def test_concurrent_first_open_constructs_once(self):
        fake_pool = AsyncFakePool()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_open():
            fake_pool.opened = True
            entered.set()
            await release.wait()

        fake_pool.open = delayed_open

        async def race_open():
            with patch(
                "agents.agent_api.app.db.settings",
                SimpleNamespace(postgres_dsn="postgresql://fake"),
            ), patch(
                "psycopg_pool.AsyncConnectionPool",
                return_value=fake_pool,
            ) as constructor:
                first = asyncio.create_task(db_module.open_async_pool())
                await entered.wait()
                second = asyncio.create_task(db_module.open_async_pool())
                await asyncio.sleep(0)
                release.set()
                results = await asyncio.gather(first, second)
                await db_module.close_async_pool()
            return results, constructor.call_count

        results, constructor_calls = asyncio.run(race_open())

        assert results == [fake_pool, fake_pool]
        assert constructor_calls == 1

    def test_first_open_from_another_thread_fails_closed(self):
        fake_pool = AsyncFakePool()
        create_entered = threading.Event()
        release_create = threading.Event()
        results = []
        errors = []

        async def create_pool(_dsn):
            create_entered.set()
            await asyncio.to_thread(release_create.wait)
            return fake_pool

        def open_pool():
            try:
                results.append(asyncio.run(db_module.open_async_pool()))
            except BaseException as error:
                errors.append(error)

        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch.object(db_module, "_create_async_pool", side_effect=create_pool) as create:
            first = threading.Thread(target=open_pool)
            first.start()
            assert create_entered.wait(timeout=2)
            second = threading.Thread(target=open_pool)
            second.start()
            second.join(timeout=2)
            release_create.set()
            first.join(timeout=2)

        assert not first.is_alive() and not second.is_alive()
        assert results == [fake_pool]
        assert len(errors) == 1
        assert "another loop" in str(errors[0])
        assert create.call_count == 1

        db_module._async_pool = None
        db_module._async_pool_loop = None

    def test_get_before_open_fails_closed(self):
        with pytest.raises(RuntimeError, match="Async DB pool is not open"):
            db_module.get_async_pool()

    def test_failed_wait_closes_partial_pool_and_allows_retry(self):
        bad_pool = FailingAsyncWaitPool()
        good_pool = AsyncFakePool()
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "psycopg_pool.AsyncConnectionPool",
            side_effect=[bad_pool, good_pool],
        ):
            with pytest.raises(RuntimeError, match="async timeout"):
                asyncio.run(db_module.open_async_pool())
            async def retry_and_close():
                result = await db_module.open_async_pool()
                await db_module.close_async_pool()
                return result

            result = asyncio.run(retry_and_close())

        assert bad_pool.closed is True
        assert result is good_pool

    def test_open_pool_is_not_reused_by_another_event_loop(self):
        fake_pool = AsyncFakePool()
        with patch(
            "agents.agent_api.app.db.settings",
            SimpleNamespace(postgres_dsn="postgresql://fake"),
        ), patch(
            "psycopg_pool.AsyncConnectionPool",
            return_value=fake_pool,
        ):
            asyncio.run(db_module.open_async_pool())
            with pytest.raises(RuntimeError, match="different event loop"):
                asyncio.run(db_module.open_async_pool())

        # The first loop is gone, so test cleanup resets the fake explicitly.
        db_module._async_pool = None
        db_module._async_pool_loop = None

    def test_invalidated_open_task_cannot_republish_pool(self):
        fake_pool = AsyncFakePool()

        async def invalidate_before_publish():
            completed_open = asyncio.create_task(asyncio.sleep(0, result=fake_pool))
            await completed_open
            db_module._async_pool_open_task = completed_open

            async def invalidate(_task):
                db_module._async_pool_open_task = None
                return fake_pool

            with patch(
                "agents.agent_api.app.db.settings",
                SimpleNamespace(postgres_dsn="postgresql://fake"),
            ), patch("asyncio.shield", side_effect=invalidate):
                with pytest.raises(RuntimeError, match="invalidated"):
                    await db_module.open_async_pool()

        asyncio.run(invalidate_before_publish())

        assert db_module._async_pool is None

    def test_close_is_idempotent_and_resets_before_awaiting(self):
        fake_pool = AsyncFakePool()

        async def close_twice():
            db_module._async_pool = fake_pool
            db_module._async_pool_loop = asyncio.get_running_loop()
            await db_module.close_async_pool()
            await db_module.close_async_pool()

        asyncio.run(close_twice())

        assert fake_pool.closed is True
        assert db_module._async_pool is None

    def test_close_failure_propagates_but_still_resets(self):
        async def close_failing_pool():
            db_module._async_pool = FailingAsyncClosePool()
            db_module._async_pool_loop = asyncio.get_running_loop()
            await db_module.close_async_pool()

        with pytest.raises(RuntimeError, match="async close failed"):
            asyncio.run(close_failing_pool())

        assert db_module._async_pool is None
