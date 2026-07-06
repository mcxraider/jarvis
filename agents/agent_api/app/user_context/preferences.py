"""Versioned validation models for database-backed assistant preferences."""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Provider = Literal["todoist", "google_calendar"]


class PreferenceConfigurationError(RuntimeError):
    """Stored preferences are absent, unsupported, or fail validation."""


class CommunicationPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: Literal["casual", "neutral", "professional"] = "neutral"
    verbosity: Literal["concise", "balanced", "detailed"] = "balanced"


class RoutingPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_provider: Provider
    event_provider: Provider
    calendar_usage: Literal["default", "explicit_only"]
    reminder_provider: Optional[Provider] = None
    time_related_provider: Optional[Provider] = None
    explicit_calendar_provider: Optional[Provider] = None


class TodoistPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: Optional[Literal["tasks_todos_reminders", "tasks_and_scheduling"]] = None
    default_for: List[str] = Field(default_factory=list)


class GoogleCalendarPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: Optional[Literal["events_meetings_time_related_items", "explicit_only"]] = None
    event_category_defaults: Dict[str, str] = Field(default_factory=dict)


class DomainPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todoist: TodoistPreferences = Field(default_factory=TodoistPreferences)
    google_calendar: GoogleCalendarPreferences = Field(
        default_factory=GoogleCalendarPreferences
    )


class AssistantPreferencesV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    communication: CommunicationPreferences
    routing: RoutingPreferences
    domains: DomainPreferences


class ResolvedUserPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    schema_version: int = Field(gt=0)
    revision: int = Field(gt=0)
    preferences: AssistantPreferencesV1

    @classmethod
    def from_database_row(cls, row: tuple) -> "ResolvedUserPreferences":
        user_id, schema_version, revision, preferences = row
        if schema_version != 1:
            raise PreferenceConfigurationError(
                f"Unsupported user preference schema version: {schema_version}."
            )
        try:
            return cls.model_validate(
                {
                    "user_id": str(user_id),
                    "schema_version": schema_version,
                    "revision": revision,
                    "preferences": preferences,
                }
            )
        except ValidationError as exc:
            raise PreferenceConfigurationError(
                "Stored user preferences failed schema validation."
            ) from exc
