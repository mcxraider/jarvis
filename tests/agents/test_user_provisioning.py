"""Tests for runtime user identity resolution (user_context.identity)."""

import pytest

from agents.agent_api.app.user_context.identity import (
    TelegramIdentity,
    refresh_identity_profile,
    resolve_active_identity,
)
from agents.agent_api.app.user_context.preferences import PreferenceConfigurationError
from agents.agent_api.app.user_context.runtime import RuntimeContextError

_VALID_PREFERENCES = {
    "communication": {"tone": "casual", "verbosity": "concise"},
    "routing": {
        "task_provider": "todoist",
        "event_provider": "google_calendar",
        "calendar_usage": "default",
    },
    "domains": {"todoist": {}, "google_calendar": {"event_category_defaults": {}}},
}
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
        assert "UPDATE public.telegram_identities" in sql
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


class TestResolveActiveIdentity:
    def test_reads_profile_and_validated_preferences(self):
        cursor = FakeCursor(
            row=("user-id", "Zachary", "Asia/Singapore", "en", 1, 3, _VALID_PREFERENCES)
        )
        identity = resolve_active_identity(cursor, IDENTITY)

        assert identity.user_id == "user-id"
        assert identity.display_name == "Zachary"
        assert identity.timezone == "Asia/Singapore"
        assert identity.preferences.revision == 3
        assert identity.preferences.preferences.routing.event_provider == "google_calendar"

    def test_no_active_user_fails_closed(self):
        cursor = FakeCursor(row=None)
        with pytest.raises(RuntimeContextError):
            resolve_active_identity(cursor, IDENTITY)

    def test_unknown_preference_version_fails_closed(self):
        cursor = FakeCursor(
            row=("user-id", "Zachary", "Asia/Singapore", "en", 2, 1, _VALID_PREFERENCES)
        )
        with pytest.raises(PreferenceConfigurationError):
            resolve_active_identity(cursor, IDENTITY)

    def test_malformed_preferences_fail_closed(self):
        cursor = FakeCursor(
            row=("user-id", "Zachary", "Asia/Singapore", "en", 1, 1, "not a dict")
        )
        with pytest.raises(PreferenceConfigurationError):
            resolve_active_identity(cursor, IDENTITY)
