"""Stage 1 tests for idempotency stores and configuration."""

from __future__ import annotations

import os
import threading
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from agents.agent_api.app.config import load_settings
from agents.agent_api.app.idempotency import create_default_idempotency_store
from agents.agent_api.app.idempotency.postgres import PostgresIdempotencyStore
from agents.agent_api.app.idempotency.store import ClaimState, MemoryIdempotencyStore


class MutableClock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class TestMemoryIdempotencyStore:
    def test_claim_complete_and_completed_hit(self):
        store = MemoryIdempotencyStore()
        claim = store.claim("key", "request", 60, 10)

        assert claim.state is ClaimState.ACQUIRED
        assert claim.owner_token
        assert store.complete("key", claim.owner_token, {"status": "completed"}, 60)

        hit = store.claim("key", "request", 60, 10)
        assert hit.state is ClaimState.COMPLETED
        assert hit.result == {"status": "completed"}

    def test_active_claim_is_in_progress(self):
        store = MemoryIdempotencyStore()
        first = store.claim("key", "operation", 60, 10)
        second = store.claim("key", "operation", 60, 10)

        assert first.state is ClaimState.ACQUIRED
        assert second.state is ClaimState.IN_PROGRESS

    def test_concurrent_claim_has_exactly_one_owner(self):
        store = MemoryIdempotencyStore()
        barrier = threading.Barrier(8)
        states = []

        def claim() -> None:
            barrier.wait()
            states.append(store.claim("key", "operation", 60, 10).state)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert states.count(ClaimState.ACQUIRED) == 1
        assert states.count(ClaimState.IN_PROGRESS) == 7

    def test_expired_lease_can_be_taken_over(self):
        clock = MutableClock()
        store = MemoryIdempotencyStore(clock)
        first = store.claim("key", "operation", 60, 5)
        clock.value += 6
        replacement = store.claim("key", "operation", 60, 5)

        assert replacement.state is ClaimState.ACQUIRED
        assert replacement.owner_token != first.owner_token

    def test_stale_owner_cannot_complete_or_abandon_replacement(self):
        clock = MutableClock()
        store = MemoryIdempotencyStore(clock)
        first = store.claim("key", "operation", 60, 5)
        clock.value += 6
        replacement = store.claim("key", "operation", 60, 5)

        assert not store.complete("key", first.owner_token, {"stale": True}, 60)
        assert not store.abandon("key", first.owner_token)
        assert store.complete("key", replacement.owner_token, {"fresh": True}, 60)
        assert store.get("key") == {"fresh": True}

    def test_owner_can_renew_lease_but_stale_owner_cannot(self):
        clock = MutableClock()
        store = MemoryIdempotencyStore(clock)
        first = store.claim("key", "request", 60, 5)

        clock.value += 4
        assert store.renew("key", first.owner_token, 5)
        clock.value += 2
        assert store.claim("key", "request", 60, 5).state is ClaimState.IN_PROGRESS
        assert not store.renew("key", "stale-owner", 5)

    def test_abandon_allows_immediate_retry(self):
        store = MemoryIdempotencyStore()
        first = store.claim("key", "operation", 60, 10)

        assert store.abandon("key", first.owner_token)
        assert store.claim("key", "operation", 60, 10).state is ClaimState.ACQUIRED

    def test_values_are_defensively_copied(self):
        store = MemoryIdempotencyStore()
        claim = store.claim("key", "request", 60, 10)
        original = {"nested": {"value": 1}}
        assert store.complete("key", claim.owner_token, original, 60)

        original["nested"]["value"] = 2
        cached = store.get("key")
        cached["nested"]["value"] = 3

        assert store.get("key") == {"nested": {"value": 1}}

    def test_ttl_expiry_and_cleanup(self):
        clock = MutableClock()
        store = MemoryIdempotencyStore(clock)
        claim = store.claim("completed", "request", 5, 2)
        assert store.complete("completed", claim.owner_token, {"ok": True}, 5)
        store.claim("active", "operation", 5, 2)

        clock.value += 6
        assert store.cleanup_expired() == 2
        assert store.get("completed") is None

    @pytest.mark.parametrize("ttl,lease", [(0, 1), (1, 0), (-1, 1)])
    def test_claim_rejects_non_positive_durations(self, ttl, lease):
        with pytest.raises(ValueError):
            MemoryIdempotencyStore().claim("key", "request", ttl, lease)

    def test_claim_rejects_lease_longer_than_ttl(self):
        with pytest.raises(ValueError, match="must not exceed"):
            MemoryIdempotencyStore().claim("key", "request", 5, 6)


class FakeCursor:
    def __init__(self, existing_row=None, advisory_lock=True):
        self.statements = []
        self._row = None
        self.rowcount = 0
        self.existing_row = existing_row
        self.advisory_lock = advisory_lock

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, params))
        self._row = None
        self.rowcount = 0
        if normalized.startswith("INSERT INTO idempotency_results"):
            if self.existing_row is None:
                self._row = ("owner",)
                self.rowcount = 1
        elif normalized.startswith("SELECT status, result_json"):
            self._row = self.existing_row
        elif normalized.startswith("SELECT pg_try_advisory_xact_lock"):
            self._row = (self.advisory_lock,)
        elif normalized.startswith("UPDATE idempotency_results SET layer ="):
            self._row = ("replacement-owner",)
            self.rowcount = 1
        elif normalized.startswith(
            "UPDATE idempotency_results SET lease_expires_at ="
        ):
            self.rowcount = 1
        elif normalized.startswith("UPDATE idempotency_results SET status = 'completed'"):
            self.rowcount = 1
        elif normalized.startswith("DELETE FROM idempotency_results WHERE expires_at"):
            self.rowcount = 3

    def fetchone(self):
        row = self._row
        self._row = None
        return row


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def transaction(self):
        return nullcontext()


class FakePool:
    def __init__(self, existing_row=None, advisory_lock=True, **kwargs):
        self.kwargs = kwargs
        self.cursor_instance = FakeCursor(
            existing_row=existing_row,
            advisory_lock=advisory_lock,
        )
        self.opened = False
        self.waited = False

    def open(self):
        self.opened = True

    def wait(self):
        self.waited = True

    def connection(self):
        return FakeConnection(self.cursor_instance)


class TestPostgresIdempotencyStore:
    def test_constructor_is_lazy_and_runtime_emits_no_schema_ddl(self):
        pools = []

        def factory(**kwargs):
            pool = FakePool(**kwargs)
            pools.append(pool)
            return pool

        store = PostgresIdempotencyStore("postgresql://example", pool_factory=factory)
        assert pools == []

        claim = store.claim("key", "operation", 60, 10, "add_todoist_task")

        assert claim.state is ClaimState.ACQUIRED
        assert len(pools) == 1
        assert pools[0].opened and pools[0].waited
        statements = [
            statement for statement, _ in pools[0].cursor_instance.statements
        ]
        assert len(statements) == 1
        assert statements[0].startswith("INSERT INTO idempotency_results")
        assert not any(
            statement.startswith(("CREATE", "ALTER", "DROP", "TRUNCATE"))
            for statement in statements
        )

    def test_pool_opens_only_once(self):
        pool = FakePool()
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )

        store.claim("one", "request", 60, 10)
        store.claim("two", "request", 60, 10)

        assert pool.opened and pool.waited
        assert len(pool.cursor_instance.statements) == 2

    def test_completed_claim_returns_cached_result(self):
        pool = FakePool(
            existing_row=("completed", {"status": "completed"}, False, False)
        )
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )

        result = store.claim("key", "request", 60, 10)

        assert result.state is ClaimState.COMPLETED
        assert result.result == {"status": "completed"}

    def test_active_claim_reports_in_progress(self):
        pool = FakePool(existing_row=("in_progress", None, False, False))
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )

        assert store.claim("key", "operation", 60, 10).state is ClaimState.IN_PROGRESS

    def test_expired_lease_is_taken_over(self):
        pool = FakePool(existing_row=("in_progress", None, True, False))
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )

        result = store.claim("key", "operation", 60, 10)

        assert result.state is ClaimState.ACQUIRED
        assert result.owner_token
        sql = "\n".join(statement for statement, _ in pool.cursor_instance.statements)
        assert "result_json = NULL" in sql
        assert "created_at = NOW()" in sql

    def test_complete_and_abandon_are_owner_scoped_in_sql(self):
        pool = FakePool()
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )
        store.claim("key", "operation", 60, 10)

        assert store.complete("key", "owner", {"ok": True}, 60)
        assert not store.abandon("key", "stale-owner")
        sql = "\n".join(statement for statement, _ in pool.cursor_instance.statements)
        assert "AND owner_token = %s" in sql

    def test_renew_is_owner_scoped_and_requires_unexpired_claim(self):
        pool = FakePool()
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )
        store.claim("key", "request", 60, 10)

        assert store.renew("key", "owner", 10)
        sql = "\n".join(statement for statement, _ in pool.cursor_instance.statements)
        assert "SET lease_expires_at =" in sql
        assert "AND owner_token = %s" in sql
        assert "AND expires_at > NOW()" in sql

    def test_connection_failure_is_fail_open(self):
        def broken_factory(**_kwargs):
            raise RuntimeError("database unavailable")

        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=broken_factory,
        )

        assert store.claim("key", "request", 60, 10).state is ClaimState.UNAVAILABLE
        assert store.get("key") is None
        assert not store.complete("key", "owner", {"ok": True}, 60)
        assert not store.abandon("key", "owner")
        assert store.cleanup_expired() == 0

    def test_cleanup_uses_advisory_lock_and_returns_delete_count(self):
        pool = FakePool()
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )

        assert store.cleanup_expired() == 3
        statements = [statement for statement, _ in pool.cursor_instance.statements]
        lock_index = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("SELECT pg_try_advisory_xact_lock")
        )
        delete_index = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("DELETE FROM idempotency_results")
        )
        assert lock_index < delete_index

    def test_cleanup_skips_when_another_worker_owns_lock(self):
        pool = FakePool(advisory_lock=False)
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )

        assert store.cleanup_expired() == 0
        assert not any(
            statement.startswith("DELETE FROM idempotency_results")
            for statement, _ in pool.cursor_instance.statements
        )

    def test_claim_rejects_lease_longer_than_ttl(self):
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: FakePool(),
        )
        with pytest.raises(ValueError, match="must not exceed"):
            store.claim("key", "request", 5, 6)


class TestIdempotencyConfiguration:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings()

        assert settings.idempotency_request_ttl_seconds == 14400
        assert settings.idempotency_operation_ttl_seconds == 7200
        assert settings.idempotency_lease_seconds == 60
        assert settings.idempotency_wait_seconds == 30.0
        assert settings.idempotency_poll_interval_seconds == 0.1
        assert settings.idempotency_cleanup_interval_seconds == 1800

    def test_overrides(self):
        with patch.dict(
            "os.environ",
            {
                "JARVIS_IDEMPOTENCY_REQUEST_TTL_SECONDS": "100",
                "JARVIS_IDEMPOTENCY_OPERATION_TTL_SECONDS": "90",
                "JARVIS_IDEMPOTENCY_LEASE_SECONDS": "20",
                "JARVIS_IDEMPOTENCY_WAIT_SECONDS": "5.5",
                "JARVIS_IDEMPOTENCY_POLL_INTERVAL_SECONDS": "0.25",
                "JARVIS_IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS": "120",
            },
            clear=False,
        ):
            settings = load_settings()

        assert settings.idempotency_request_ttl_seconds == 100
        assert settings.idempotency_operation_ttl_seconds == 90
        assert settings.idempotency_lease_seconds == 20
        assert settings.idempotency_wait_seconds == 5.5
        assert settings.idempotency_poll_interval_seconds == 0.25
        assert settings.idempotency_cleanup_interval_seconds == 120

    @pytest.mark.parametrize(
        "name",
        [
            "JARVIS_IDEMPOTENCY_REQUEST_TTL_SECONDS",
            "JARVIS_IDEMPOTENCY_OPERATION_TTL_SECONDS",
            "JARVIS_IDEMPOTENCY_LEASE_SECONDS",
            "JARVIS_IDEMPOTENCY_WAIT_SECONDS",
            "JARVIS_IDEMPOTENCY_POLL_INTERVAL_SECONDS",
            "JARVIS_IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS",
        ],
    )
    def test_non_positive_values_are_rejected(self, name):
        with patch.dict("os.environ", {name: "0"}, clear=False):
            with pytest.raises(ValueError, match="greater than zero"):
                load_settings()

    def test_lease_cannot_exceed_configured_ttls(self):
        with patch.dict(
            "os.environ",
            {
                "JARVIS_IDEMPOTENCY_REQUEST_TTL_SECONDS": "10",
                "JARVIS_IDEMPOTENCY_OPERATION_TTL_SECONDS": "5",
                "JARVIS_IDEMPOTENCY_LEASE_SECONDS": "6",
            },
            clear=False,
        ):
            with pytest.raises(ValueError, match="must not exceed"):
                load_settings()

    def test_factory_uses_memory_without_dsn(self):
        assert isinstance(
            create_default_idempotency_store(postgres_dsn=""),
            MemoryIdempotencyStore,
        )

    def test_factory_returns_lazy_postgres_store_with_dsn(self):
        store = create_default_idempotency_store("postgresql://unreachable")
        assert isinstance(store, PostgresIdempotencyStore)
        assert store._pool is None


class TestPostgresIdempotencyStorePoolHardening:
    def test_close_resets_pool_to_none(self):
        pool = FakePool()
        pool.close = lambda: None
        store = PostgresIdempotencyStore(
            "postgresql://example",
            pool_factory=lambda **_kwargs: pool,
        )
        store.claim("key", "request", 60, 10)
        assert store._pool is not None
        store.close()
        assert store._pool is None

    def test_close_is_safe_when_pool_not_opened(self):
        store = PostgresIdempotencyStore("postgresql://example")
        store.close()
        assert store._pool is None

    def test_fallback_factory_receives_health_check_params(self):
        """When pool_factory is provided, it still gets check/max_idle/max_lifetime."""
        received_kwargs = {}

        def capturing_factory(**kwargs):
            received_kwargs.update(kwargs)
            return FakePool(**kwargs)

        store = PostgresIdempotencyStore(
            "postgresql://example", pool_factory=capturing_factory
        )
        store.claim("key", "request", 60, 10)

        assert received_kwargs["max_idle"] == 300
        assert received_kwargs["max_lifetime"] == 1800
        assert received_kwargs["min_size"] == 2
        assert received_kwargs["max_size"] == 5
        assert callable(received_kwargs["check"])

    def test_default_factory_sets_health_check_params(self):
        """When no pool_factory is provided, the pool gets check/max_idle/max_lifetime."""
        from unittest.mock import patch as _patch, MagicMock

        mock_pool_cls = MagicMock()
        mock_pool_cls.check_connection = "check_sentinel"
        mock_pool = FakePool()
        mock_pool.close = lambda: None
        mock_pool_cls.return_value = mock_pool

        store = PostgresIdempotencyStore("postgresql://example")
        with _patch.dict(
            "sys.modules",
            {"psycopg_pool": MagicMock(ConnectionPool=mock_pool_cls)},
        ):
            store._ensure_pool()
        call_kwargs = mock_pool_cls.call_args[1]
        assert call_kwargs["max_idle"] == 300
        assert call_kwargs["max_lifetime"] == 1800
        assert call_kwargs["min_size"] == 2
        assert call_kwargs["max_size"] == 5
        assert call_kwargs["check"] == "check_sentinel"
        store.close()
