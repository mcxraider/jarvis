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
        missing_columns=(),
        missing_privileges=(),
    ):
        self.results = iter(
            [
                [("jarvis_app", True)],
                [(value,) for value in missing_tables],
                [(value,) for value in missing_columns],
                [(value,) for value in missing_privileges],
            ]
        )
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        if statement.lstrip().startswith("SELECT 1 FROM public."):
            self.rows = []
        else:
            self.rows = next(self.results)

    def fetchone(self):
        return self.rows[0]

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


@pytest.mark.parametrize(
    "cursor_options",
    [
        {"missing_tables": ("idempotency_results",)},
        {"missing_columns": ("lease_expires_at",)},
        {"missing_privileges": ("DELETE",)},
    ],
)
def test_readiness_rejects_incomplete_idempotency_provisioning(cursor_options):
    with pytest.raises(RuntimeError, match="Database runtime readiness failed"):
        run_readiness(**cursor_options)
