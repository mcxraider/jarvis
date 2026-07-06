"""Orchestration + resume tests for user_context.resolver.

The leaf modules (identity, preferences, secrets, domains) each have focused unit
tests. This file pins the *seam* that threads them together in one DB connection:

* ``resolve_runtime_context`` — fresh resolution (gate -> identity -> connections ->
  classify -> secret-free snapshot), credentials only for active domains.
* ``load_thread_runtime_context`` — the resume invariant: a stored snapshot is reused
  verbatim and every saved active capability is re-validated, failing closed on drift.
* ``store_thread_context`` — the persisted snapshot carries no secrets.

Everything is driven through a scripted fake cursor so no Postgres is required; the
real ``classify_domains`` / ``DOMAIN_ADAPTERS`` / preference validation run.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.agent_api.app.user_context import resolver as resolver_module
from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1
from agents.agent_api.app.user_context.identity import TelegramIdentity
from agents.agent_api.app.user_context.resolver import (
    load_thread_runtime_context,
    resolve_runtime_context,
    store_thread_context,
)
from agents.agent_api.app.user_context.runtime import (
    DomainAvailability,
    RuntimeContextError,
    RuntimeContextSnapshot,
)

USER_ID = "11111111-1111-1111-1111-111111111111"
TELEGRAM_ID = 4242
IDENTITY = TelegramIdentity(
    telegram_id=TELEGRAM_ID,
    username="tester",
)
TODOIST_SECRET = "SENTINEL-TODOIST-SECRET"

_VALID_PREFERENCES = {
    "communication": {"tone": "casual", "verbosity": "concise"},
    "routing": {
        "task_provider": "todoist",
        "event_provider": "google_calendar",
        "calendar_usage": "default",
    },
    "domains": {"todoist": {}, "google_calendar": {"event_category_defaults": {}}},
}


class ScriptedCursor:
    """Replays queued ``fetchone``/``fetchall`` results and records statements.

    ``fetchone_rows`` are popped in call order; ``fetchall_rows`` likewise. This lets
    a single cursor stand in for the whole resolver sequence (gate UPDATE, identity
    SELECT, connections SELECT, per-secret SELECTs).
    """

    def __init__(self, *, fetchone_rows=None, fetchall_rows=None):
        self.statements = []
        self._fetchone = list(fetchone_rows or [])
        self._fetchall = list(fetchall_rows or [])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((" ".join(statement.split()), params))

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []


class ScriptedConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class ScriptedPool:
    """Hands out one connection and counts how many times it is opened."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.connection_opens = 0

    def connection(self):
        self.connection_opens += 1
        return ScriptedConnection(self._cursor)


def _patch_pool(cursor):
    pool = ScriptedPool(cursor)
    return patch.object(resolver_module, "get_pool", return_value=pool), pool


def _snapshot(*, domains, prefs=None):
    return RuntimeContextSnapshot(
        user_id=USER_ID,
        display_name="Tester",
        timezone="Asia/Singapore",
        locale="en",
        preference_schema_version=1,
        preference_revision=2,
        preferences=AssistantPreferencesV1.model_validate(prefs or _VALID_PREFERENCES),
        domains=domains,
        resolved_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
    )


# --------------------------------------------------------------------------- #
# resolve_runtime_context — fresh resolution orchestration (#14)
# --------------------------------------------------------------------------- #


class TestResolveRuntimeContext:
    def _cursor(self):
        # fetchone order: gate UPDATE -> identity SELECT -> todoist secret SELECT.
        # fetchall order: connections SELECT.
        profile_row = (
            USER_ID,
            "Tester",
            "Asia/Singapore",
            "en",
            1,  # schema_version
            2,  # revision
            _VALID_PREFERENCES,
        )
        connection_rows = [
            ("todoist-conn", "todoist", "connected", True),
            ("calendar-conn", "google_calendar", "connected", False),  # disabled
        ]
        return ScriptedCursor(
            fetchone_rows=[(USER_ID,), profile_row, (TODOIST_SECRET,)],
            fetchall_rows=[connection_rows],
        )

    def test_builds_snapshot_with_only_active_credentials(self):
        cursor = self._cursor()
        patcher, pool = _patch_pool(cursor)
        with patcher:
            resolved = resolve_runtime_context(IDENTITY)

        snapshot = resolved.snapshot
        assert snapshot.user_id == USER_ID
        assert snapshot.active_providers() == {"todoist"}
        # Disabled calendar connection is surfaced as unavailable, not dropped.
        calendar = {d.provider: d for d in snapshot.domains}["google_calendar"]
        assert (calendar.status, calendar.reason) == ("unavailable", "disabled")
        # Credentials exist only for active domains.
        assert set(resolved.credentials) == {"todoist"}
        assert resolved.credentials["todoist"].secret == TODOIST_SECRET

    def test_resolution_uses_a_single_connection(self):
        cursor = self._cursor()
        patcher, pool = _patch_pool(cursor)
        with patcher:
            resolve_runtime_context(IDENTITY)
        assert pool.connection_opens == 1

    def test_snapshot_serialization_contains_no_secret(self):
        cursor = self._cursor()
        patcher, _pool = _patch_pool(cursor)
        with patcher:
            resolved = resolve_runtime_context(IDENTITY)
        assert TODOIST_SECRET not in resolved.snapshot.model_dump_json()

    def test_gate_rejection_propagates(self):
        # Gate UPDATE returns no row -> refresh_identity_profile raises PermissionError.
        cursor = ScriptedCursor(fetchone_rows=[None])
        patcher, _pool = _patch_pool(cursor)
        with patcher, pytest.raises(PermissionError):
            resolve_runtime_context(IDENTITY)


# --------------------------------------------------------------------------- #
# load_thread_runtime_context — resume invariant (#12)
# --------------------------------------------------------------------------- #


def _active_todoist_snapshot(connection_id="todoist-conn"):
    return _snapshot(
        domains=[
            DomainAvailability(
                provider="todoist",
                status="active",
                connection_id=connection_id,
                capabilities=["tasks"],
            )
        ]
    )


class TestLoadThreadRuntimeContext:
    def test_reuses_stored_snapshot_verbatim_and_rehydrates_secret(self):
        stored = _active_todoist_snapshot()
        cursor = ScriptedCursor(
            fetchone_rows=[
                (USER_ID,),  # gate
                (stored.model_dump(mode="json"),),  # stored snapshot (jsonb -> dict)
                (USER_ID, "todoist", "connected", True),  # live connection re-check
                (TODOIST_SECRET,),  # secret rehydration
            ]
        )
        patcher, _pool = _patch_pool(cursor)
        with patcher:
            resolved = load_thread_runtime_context("thread-1", IDENTITY)

        # The interrupted thread is resolved from its snapshot, not re-derived from
        # current profile/preferences: the returned snapshot equals what was stored.
        assert resolved.snapshot == stored
        assert set(resolved.credentials) == {"todoist"}
        assert resolved.credentials["todoist"].secret == TODOIST_SECRET

    def test_only_active_domains_are_rehydrated(self):
        stored = _snapshot(
            domains=[
                DomainAvailability(
                    provider="todoist",
                    status="active",
                    connection_id="todoist-conn",
                    capabilities=["tasks"],
                ),
                DomainAvailability(
                    provider="google_calendar",
                    status="unavailable",
                    reason="not_connected",
                    capabilities=["calendar_events"],
                ),
            ]
        )
        cursor = ScriptedCursor(
            fetchone_rows=[
                (USER_ID,),
                (stored.model_dump(mode="json"),),
                (USER_ID, "todoist", "connected", True),
                (TODOIST_SECRET,),
            ]
        )
        patcher, _pool = _patch_pool(cursor)
        with patcher:
            resolved = load_thread_runtime_context("thread-1", IDENTITY)
        # The unavailable calendar domain triggers no connection/secret lookup.
        assert set(resolved.credentials) == {"todoist"}

    def test_missing_snapshot_fails_closed(self):
        cursor = ScriptedCursor(fetchone_rows=[(USER_ID,), None])
        patcher, _pool = _patch_pool(cursor)
        with patcher, pytest.raises(RuntimeContextError):
            load_thread_runtime_context("thread-1", IDENTITY)

    def test_disabled_saved_capability_fails_closed(self):
        stored = _active_todoist_snapshot()
        cursor = ScriptedCursor(
            fetchone_rows=[
                (USER_ID,),
                (stored.model_dump(mode="json"),),
                (USER_ID, "todoist", "connected", False),  # now disabled
            ]
        )
        patcher, _pool = _patch_pool(cursor)
        with patcher, pytest.raises(RuntimeContextError):
            load_thread_runtime_context("thread-1", IDENTITY)

    def test_revoked_status_saved_capability_fails_closed(self):
        stored = _active_todoist_snapshot()
        cursor = ScriptedCursor(
            fetchone_rows=[
                (USER_ID,),
                (stored.model_dump(mode="json"),),
                (USER_ID, "todoist", "needs_reauth", True),  # no longer connected
            ]
        )
        patcher, _pool = _patch_pool(cursor)
        with patcher, pytest.raises(RuntimeContextError):
            load_thread_runtime_context("thread-1", IDENTITY)

    def test_unresolvable_saved_secret_fails_closed(self):
        stored = _active_todoist_snapshot()
        cursor = ScriptedCursor(
            fetchone_rows=[
                (USER_ID,),
                (stored.model_dump(mode="json"),),
                (USER_ID, "todoist", "connected", True),
                (None,),  # secret cannot be resolved
            ]
        )
        patcher, _pool = _patch_pool(cursor)
        with patcher, pytest.raises(RuntimeContextError):
            load_thread_runtime_context("thread-1", IDENTITY)

    def test_active_domain_without_connection_id_fails_closed(self):
        stored = _snapshot(
            domains=[
                DomainAvailability(
                    provider="todoist", status="active", capabilities=["tasks"]
                )
            ]
        )
        cursor = ScriptedCursor(
            fetchone_rows=[(USER_ID,), (stored.model_dump(mode="json"),)]
        )
        patcher, _pool = _patch_pool(cursor)
        with patcher, pytest.raises(RuntimeContextError):
            load_thread_runtime_context("thread-1", IDENTITY)


# --------------------------------------------------------------------------- #
# store_thread_context — persisted snapshot is secret-free (#11 write path)
# --------------------------------------------------------------------------- #


class TestStoreThreadContext:
    def test_persists_secret_free_snapshot_payload(self):
        snapshot = _active_todoist_snapshot()
        cursor = ScriptedCursor()
        patcher, _pool = _patch_pool(cursor)
        with patcher:
            store_thread_context("thread-1", "add milk to my list", snapshot)

        assert len(cursor.statements) == 1
        sql, params = cursor.statements[0]
        assert "INSERT INTO public.threads" in sql
        # The serialized snapshot is params[3]; it must carry versioning but no secret.
        serialized = params[3]
        assert TODOIST_SECRET not in serialized
        assert '"schema_version"' in serialized
        assert params[1] == snapshot.user_id
        assert params[5] == snapshot.preference_revision
