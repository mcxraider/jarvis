"""Tests for runtime user identity resolution (user_context.identity)."""

import pytest

from agents.agent_api.app.user_context.identity import (
    TelegramIdentity,
    refresh_identity_profile,
    resolve_active_identity,
)
from agents.agent_api.app.user_context.runtime import (
    PolicyShadowMismatchError,
    RuntimeContextError,
)

IDENTITY = TelegramIdentity(
    telegram_id=42,
    username="tester",
)


class FakeCursor:
    """Records statements and returns a preset (or per-call) fetchone row."""

    def __init__(self, row=None, rows=None):
        self.statements = []
        self._row = row
        self._rows = list(rows) if rows is not None else None

    def execute(self, statement, params=None):
        self.statements.append((" ".join(statement.split()), params))

    def fetchone(self):
        if self._rows is not None:
            return self._rows.pop(0)
        return self._row


class TestRefreshIdentityProfile:
    def test_returns_user_id_and_updates_identity(self):
        cursor = FakeCursor(row=("user-id",))
        result = refresh_identity_profile(cursor, IDENTITY)

        assert result == "user-id"
        sql, params = cursor.statements[0]
        assert "UPDATE public.users" in sql
        assert "telegram_last_seen_at = NOW()" in sql
        assert "app_user.status = 'active'" in sql
        assert params == ("tester", 42)

    def test_missing_active_identity_fails_closed(self):
        cursor = FakeCursor(row=None)
        with pytest.raises(PermissionError):
            refresh_identity_profile(cursor, IDENTITY)

    def test_optional_cli_profile_values_have_explicit_postgres_types(self):
        cursor = FakeCursor(row=("user-id",))

        result = refresh_identity_profile(
            cursor, TelegramIdentity(telegram_id=42)
        )

        assert result == "user-id"
        sql, params = cursor.statements[0]
        assert sql.count("%s::text") == 1
        assert params == (None, 42)

    def test_last_seen_update_does_not_touch_preferences(self):
        cursor = FakeCursor(row=("user-id",))
        refresh_identity_profile(cursor, IDENTITY)

        sql, _params = cursor.statements[0]
        assert "preferences" not in sql
        assert "preference_revision" not in sql


class TestResolveActiveIdentity:
    def test_reads_profile_and_validated_policy(self):
        cursor = FakeCursor(
            row=(
                "user-id",
                "Zachary",
                "Asia/Singapore",
                "en",
                3,
                "gpt-test",
                "high",
                12,
                False,
                "Use Todoist for tasks.\nBe concise.",
                [{"id": "project-1", "label": "Secret", "is_primary": False}],
                [],
                True,
                True,
            )
        )
        identity = resolve_active_identity(cursor, IDENTITY)

        assert identity.user_id == "user-id"
        assert identity.display_name == "Zachary"
        assert identity.timezone == "Asia/Singapore"
        assert identity.policy_revision == 3
        assert identity.runtime_policy.forced_model == "gpt-test"
        assert identity.runtime_policy.max_agent_turns == 12
        assert identity.resource_restrictions.restricted_todoist_projects[0].id == "project-1"
        assert identity.custom_instructions == "Use Todoist for tasks.\nBe concise."
        sql, _params = cursor.statements[0]
        assert "FROM public.users app_user" in sql
        assert "private.user_runtime_policies" in sql
        assert "private.runtime_policy_shadow_status" in sql
        assert "app_user.preferences" not in sql

    def test_shadow_mismatch_fails_closed_without_reading_legacy_document(self):
        cursor = FakeCursor(
            row=(
                "user-id", "Zachary", "Asia/Singapore", "en", 1,
                None, None, None, None, "instructions", [], [], True, False,
            )
        )

        with pytest.raises(
            PolicyShadowMismatchError, match="shadow comparison failed"
        ) as raised:
            resolve_active_identity(cursor, IDENTITY)

        assert raised.value.runtime_matches is True
        assert raised.value.access_matches is False
        sql, _params = cursor.statements[0]
        assert "private.runtime_policy_shadow_status" in sql
        assert "app_user.preferences" not in sql

    def test_no_active_user_fails_closed(self):
        cursor = FakeCursor(row=None)
        with pytest.raises(RuntimeContextError):
            resolve_active_identity(cursor, IDENTITY)

    def test_malformed_runtime_policy_fails_closed(self):
        cursor = FakeCursor(
            row=(
                "user-id", "Zachary", "Asia/Singapore", "en", 1,
                None, "extreme", None, None, "instructions", [], [], True, True,
            )
        )
        with pytest.raises(RuntimeContextError):
            resolve_active_identity(cursor, IDENTITY)

    def test_malformed_restrictions_fail_closed(self):
        cursor = FakeCursor(
            row=(
                "user-id", "Zachary", "Asia/Singapore", "en", 1,
                None, None, None, None, "instructions", "not a list", [], True, True,
            )
        )
        with pytest.raises(RuntimeContextError):
            resolve_active_identity(cursor, IDENTITY)
