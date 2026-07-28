"""Tests for versioned preference validation and timezone threading."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agents.agent_api.app.graph.prompts.orchestrator import (
    _current_user_datetime,
    _user_timezone,
    get_orchestrator_prompt,
    get_system_prompt,
)
from agents.agent_api.app.user_context.preferences import (
    AssistantPreferencesV1,
    PreferenceConfigurationError,
    ResolvedUserPreferences,
)


class TestResolvedUserPreferences:
    valid_preferences = {
        "communication": {"tone": "casual", "verbosity": "concise"},
        "routing": {
            "task_provider": "todoist",
            "event_provider": "google_calendar",
            "calendar_usage": "default",
        },
        "domains": {
            "todoist": {},
            "google_calendar": {"event_category_defaults": {}},
        },
    }

    def test_valid_row_validates(self):
        resolved = ResolvedUserPreferences.from_database_row(
            ("user-id", 1, 3, self.valid_preferences)
        )
        assert resolved.revision == 3
        assert resolved.preferences.routing.event_provider == "google_calendar"

    def test_live_routing_and_domain_fields_validate(self):
        preferences = {
            "communication": {"tone": "casual", "verbosity": "concise"},
            "routing": {
                "task_provider": "todoist",
                "event_provider": "todoist",
                "reminder_provider": "todoist",
                "time_related_provider": "todoist",
                "explicit_calendar_provider": "google_calendar",
                "calendar_usage": "explicit_only",
            },
            "domains": {
                "todoist": {
                    "usage": "tasks_and_scheduling",
                    "default_for": ["tasks", "reminders", "events"],
                },
                "google_calendar": {
                    "usage": "explicit_only",
                    "event_category_defaults": {},
                },
            },
        }

        resolved = ResolvedUserPreferences.from_database_row(
            ("user-id", 1, 2, preferences)
        )

        assert resolved.preferences.routing.reminder_provider == "todoist"
        assert resolved.preferences.routing.explicit_calendar_provider == "google_calendar"
        assert resolved.preferences.domains.todoist.default_for == [
            "tasks",
            "reminders",
            "events",
        ]

    def test_unknown_schema_version_fails_closed(self):
        with pytest.raises(PreferenceConfigurationError):
            ResolvedUserPreferences.from_database_row(
                ("user-id", 2, 1, self.valid_preferences)
            )

    def test_malformed_preferences_fail_closed(self):
        with pytest.raises(PreferenceConfigurationError):
            ResolvedUserPreferences.from_database_row(("user-id", 1, 1, "not a dict"))

    def test_unknown_field_is_rejected(self):
        bad = {**self.valid_preferences, "unexpected": {"x": 1}}
        with pytest.raises(PreferenceConfigurationError):
            ResolvedUserPreferences.from_database_row(("user-id", 1, 1, bad))

    def test_v1_defaults_all_routing_categories(self):
        preferences = AssistantPreferencesV1.model_validate(
            {
                "communication": {"tone": "neutral", "verbosity": "balanced"},
                "routing": {
                    "task_provider": "todoist",
                    "event_provider": "todoist",
                    "calendar_usage": "explicit_only",
                },
                "domains": {"todoist": {}, "google_calendar": {}},
            }
        )
        assert preferences.routing.reminder_provider == "todoist"
        assert preferences.routing.time_related_provider == "todoist"
        assert preferences.routing.explicit_calendar_provider == "todoist"

    def test_google_calendar_can_be_a_calendar_backed_task_provider(self):
        payload = {
            "communication": {"tone": "neutral", "verbosity": "balanced"},
            "routing": {
                "task_provider": "google_calendar",
                "event_provider": "google_calendar",
                "calendar_usage": "default",
            },
            "domains": {"todoist": {}, "google_calendar": {}},
        }
        preferences = AssistantPreferencesV1.model_validate(payload)
        assert preferences.routing.task_provider == "google_calendar"
        assert preferences.routing.reminder_provider == "google_calendar"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"task_provider": "google_calendar"},
            {"event_provider": "google_calendar"},
            {"reminder_provider": "google_calendar"},
            {"time_related_provider": "google_calendar"},
        ],
    )
    def test_explicit_only_rejects_implicit_google_calendar_defaults(
        self,
        overrides,
    ):
        payload = {
            "communication": {"tone": "neutral", "verbosity": "balanced"},
            "routing": {
                "task_provider": "todoist",
                "event_provider": "todoist",
                "calendar_usage": "explicit_only",
                **overrides,
            },
            "domains": {"todoist": {}, "google_calendar": {}},
        }
        with pytest.raises(ValueError, match="explicit_only conflicts"):
            AssistantPreferencesV1.model_validate(payload)

    def test_explicit_only_allows_explicit_calendar_google_default(self):
        preferences = AssistantPreferencesV1.model_validate(
            {
                "communication": {"tone": "neutral", "verbosity": "balanced"},
                "routing": {
                    "task_provider": "todoist",
                    "event_provider": "todoist",
                    "calendar_usage": "explicit_only",
                    "explicit_calendar_provider": "google_calendar",
                },
                "domains": {"todoist": {}, "google_calendar": {}},
            }
        )
        assert preferences.routing.explicit_calendar_provider == "google_calendar"


class TestUserTimezone:
    def test_override_wins(self):
        assert _user_timezone("Europe/London") == "Europe/London"

    def test_env_var_used_when_no_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_USER_TIMEZONE", "Asia/Tokyo")
        assert _user_timezone(None) == "Asia/Tokyo"

    def test_falls_back_to_utc_on_detection_failure(self, monkeypatch):
        monkeypatch.delenv("JARVIS_USER_TIMEZONE", raising=False)
        with patch(
            "agents.agent_api.app.graph.prompts.orchestrator.datetime"
        ) as mock_dt:
            mock_dt.now.side_effect = Exception("no tz")
            result = _user_timezone(None)
        assert result == "UTC"

    def test_current_datetime_is_localized_to_iana_timezone(self):
        instant = datetime(2026, 7, 9, 16, 30, tzinfo=timezone.utc)
        with patch(
            "agents.agent_api.app.graph.prompts.orchestrator.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = instant
            result = _current_user_datetime("Asia/Singapore")

        assert result.isoformat() == "2026-07-10T00:30:00+08:00"

    def test_current_datetime_supports_fixed_offset_timezone(self):
        instant = datetime(2026, 7, 9, 16, 30, tzinfo=timezone.utc)
        with patch(
            "agents.agent_api.app.graph.prompts.orchestrator.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = instant
            result = _current_user_datetime("+08")

        assert result.isoformat() == "2026-07-10T00:30:00+08:00"


class TestPromptTimezoneThreading:
    def test_get_orchestrator_prompt_embeds_timezone(self):
        prompt = get_orchestrator_prompt("America/Chicago")
        assert "User timezone: America/Chicago" in prompt

    def test_get_system_prompt_threads_timezone(self):
        prompt = get_system_prompt("Asia/Taipei")
        assert "User timezone: Asia/Taipei" in prompt

    def test_build_initial_messages_threads_timezone(self):
        from agents.agent_api.app.graph.prompts.context import build_initial_messages

        messages = build_initial_messages("hello", timezone="Europe/Berlin")
        system_content = messages[0]["content"]
        assert "User timezone: Europe/Berlin" in system_content

    def test_build_initial_state_threads_timezone(self):
        from agents.agent_api.app.graph.builder import build_initial_state

        state = build_initial_state("test prompt", timezone="US/Pacific")
        system_content = state["messages"][0]["content"]
        assert "User timezone: US/Pacific" in system_content
