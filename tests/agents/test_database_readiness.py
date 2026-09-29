"""Least-privilege database startup readiness checks."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.agent_api.app import db


class ReadinessCursor:
    def __init__(
        self,
        *,
        missing_tables=(),
        missing_private_tables=(),
        missing_profile_columns=(),
        missing_columns=(),
        missing_privileges=(),
        missing_memory_contract=(),
    ):
        self.results = iter(
            [
                [("jarvis_app", True)],
                [(value,) for value in missing_tables],
                [(value,) for value in missing_private_tables],
                [(value,) for value in missing_profile_columns],
                [(value,) for value in missing_columns],
                [(value,) for value in missing_privileges],
                [(value,) for value in missing_memory_contract],
            ]
        )
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        if statement.lstrip().startswith(("SELECT 1 FROM public.", "SELECT 1 FROM private.")):
            self.rows = []
        else:
            self.rows = next(self.results)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class ReadinessPool:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def connection(self):
        cursor = self.cursor_instance

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return cursor

            def transaction(self):
                return nullcontext()

        return Connection()


def run_readiness(**cursor_options):
    pool = ReadinessPool(ReadinessCursor(**cursor_options))
    with (
        patch.object(
            db,
            "settings",
            SimpleNamespace(postgres_dsn="postgresql://configured"),
        ),
        patch.object(db, "get_pool", return_value=pool),
    ):
        db.verify_database_runtime()


def test_readiness_accepts_complete_least_privilege_schema():
    run_readiness()


def test_readiness_requires_custom_instructions_column():
    assert "custom_instructions" in db._REQUIRED_USER_PROFILE_COLUMNS
    with pytest.raises(RuntimeError, match="Database runtime readiness failed"):
        run_readiness(missing_profile_columns=("custom_instructions",))


def test_readiness_requires_typed_policy_tables_but_not_legacy_preferences():
    assert "user_runtime_policies" in db._REQUIRED_PRIVATE_TABLES
    assert "preferences" not in db._REQUIRED_USER_PROFILE_COLUMNS
    with pytest.raises(RuntimeError, match="Database runtime readiness failed"):
        run_readiness(missing_private_tables=("user_runtime_policies",))


@pytest.mark.parametrize(
    "cursor_options",
    [
        {"missing_tables": ("idempotency_results",)},
        {"missing_profile_columns": ("telegram_id",)},
        {"missing_columns": ("lease_expires_at",)},
        {"missing_privileges": ("DELETE",)},
        {"missing_memory_contract": ("function:prepare_thread_memory:EXECUTE",)},
    ],
)
def test_readiness_rejects_incomplete_idempotency_provisioning(cursor_options):
    with pytest.raises(RuntimeError, match="Database runtime readiness failed"):
        run_readiness(**cursor_options)
