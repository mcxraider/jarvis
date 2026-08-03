"""Versioned validation models for database-backed assistant preferences."""

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    model_validator,
)

Provider = Literal["todoist", "google_calendar"]
PreferenceText = str
FutureProvider = Literal[
    "github",
    "gmail",
    "google_drive",
    "apple_calendar",
    "notion",
]


class PreferenceConfigurationError(RuntimeError):
    """Stored preferences are absent, unsupported, or fail validation."""


class CommunicationPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: Literal["casual", "neutral", "professional"] = "neutral"
    verbosity: Literal["concise", "balanced", "detailed"] = "balanced"
    likes: List[PreferenceText] = Field(default_factory=list, max_length=10)
    avoid: List[PreferenceText] = Field(default_factory=list, max_length=10)
    notes: List[PreferenceText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_bounded_text(self) -> "CommunicationPreferences":
        _validate_text_lists(self.likes, self.avoid, self.notes)
        return self


class RoutingException(BaseModel):
    model_config = ConfigDict(extra="forbid")

    when: PreferenceText = Field(min_length=1, max_length=200)
    provider: Provider


class RoutingPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_provider: Provider
    event_provider: Provider
    calendar_usage: Literal["default", "explicit_only"]
    reminder_provider: Optional[Provider] = None
    time_related_provider: Optional[Provider] = None
    explicit_calendar_provider: Optional[Provider] = None
    exceptions: List[RoutingException] = Field(default_factory=list, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def apply_provider_fallbacks(cls, value):
        if not isinstance(value, dict):
            return value
        resolved = dict(value)
        task_provider = resolved.get("task_provider")
        event_provider = resolved.get("event_provider")
        if resolved.get("reminder_provider") is None:
            resolved["reminder_provider"] = task_provider
        if resolved.get("time_related_provider") is None:
            resolved["time_related_provider"] = event_provider
        if resolved.get("explicit_calendar_provider") is None:
            resolved["explicit_calendar_provider"] = event_provider
        return resolved

    @model_validator(mode="after")
    def validate_calendar_activation(self) -> "RoutingPreferences":
        if self.calendar_usage == "explicit_only":
            implicit_google_fields = [
                name
                for name in (
                    "task_provider",
                    "event_provider",
                    "reminder_provider",
                    "time_related_provider",
                )
                if getattr(self, name) == "google_calendar"
            ]
            if implicit_google_fields:
                raise ValueError(
                    "explicit_only conflicts with implicit Google Calendar routing: "
                    + ", ".join(implicit_google_fields)
                )
        return self


class TodoistPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: Optional[Literal["tasks_todos_reminders", "tasks_and_scheduling"]] = None
    default_for: List[str] = Field(default_factory=list)
    user_domain_specific_comments: List[PreferenceText] = Field(
        default_factory=list,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_domain_comments(self) -> "TodoistPreferences":
        _validate_text_lists(self.user_domain_specific_comments)
        return self


class GoogleCalendarPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: Optional[Literal["events_meetings_time_related_items", "explicit_only"]] = None
    event_category_defaults: Dict[str, str] = Field(default_factory=dict)
    fallback_calendar: Optional[PreferenceText] = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    user_domain_specific_comments: List[PreferenceText] = Field(
        default_factory=list,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_calendar_names(self) -> "GoogleCalendarPreferences":
        if len(self.event_category_defaults) > 10:
            raise ValueError("event_category_defaults may contain at most 10 entries")
        _validate_text_lists(
            list(self.event_category_defaults),
            list(self.event_category_defaults.values()),
        )
        _validate_text_lists(self.user_domain_specific_comments)
        return self


class DomainPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todoist: TodoistPreferences = Field(default_factory=TodoistPreferences)
    google_calendar: GoogleCalendarPreferences = Field(
        default_factory=GoogleCalendarPreferences
    )


class RestrictedResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=300)
    label: str = Field(min_length=1, max_length=200)
    is_primary: bool = False


class AccessPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restricted_todoist_projects: List[RestrictedResource] = Field(
        default_factory=list,
        max_length=50,
    )
    restricted_google_calendars: List[RestrictedResource] = Field(
        default_factory=list,
        max_length=50,
    )

    @model_validator(mode="after")
    def validate_unique_resources(self) -> "AccessPreferences":
        for field_name in (
            "restricted_todoist_projects",
            "restricted_google_calendars",
        ):
            resources = getattr(self, field_name)
            ids = [resource.id for resource in resources]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{field_name} contains duplicate resource IDs")
        return self

    def has_restrictions(self) -> bool:
        return bool(
            self.restricted_todoist_projects
            or self.restricted_google_calendars
        )


class OnboardingMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    future_providers: List[FutureProvider] = Field(default_factory=list, max_length=10)
    admin_notes: List[PreferenceText] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_admin_notes(self) -> "OnboardingMetadata":
        _validate_text_lists(self.admin_notes)
        return self


class LlmPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: Optional[str] = Field(default=None, min_length=1, max_length=100)
    reasoning_effort: Optional[
        Literal["off", "none", "low", "medium", "high", "xhigh", "max"]
    ] = None


class ExecutionPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_agent_turns: Optional[int] = Field(default=None, strict=True, gt=0, le=50)
    allow_mutations: Optional[StrictBool] = None


class AssistantPreferencesV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    communication: CommunicationPreferences
    routing: RoutingPreferences
    domains: DomainPreferences
    access: AccessPreferences = Field(default_factory=AccessPreferences)
    onboarding: OnboardingMetadata = Field(default_factory=OnboardingMetadata)
    llm: LlmPreferences = Field(default_factory=LlmPreferences)
    execution: ExecutionPreferences = Field(default_factory=ExecutionPreferences)


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


@dataclass(frozen=True)
class ResolvedUserRuntimeConfig:
    forced_model: Optional[str]
    forced_reasoning_effort: Optional[str]
    max_agent_turns: int
    allow_mutations: bool


def resolve_user_runtime_config(
    *,
    global_max_turns: int,
    global_allow_mutations: bool,
    llm: Optional[LlmPreferences],
    execution: Optional[ExecutionPreferences],
    request_max_turns: Optional[int],
    request_allow_mutations: Optional[bool],
) -> ResolvedUserRuntimeConfig:
    """Combine global settings, user preferences, and per-request overrides.

    Model/reasoning are forced overrides (a non-null user value wins). Turn budget
    and mutation permission can only tighten the global setting: a missing user or
    request value cannot raise a limit or re-enable a higher-level ``False``.
    """
    user_max = (
        execution.max_agent_turns
        if execution is not None and execution.max_agent_turns is not None
        else global_max_turns
    )
    request_max = request_max_turns if request_max_turns is not None else global_max_turns
    return ResolvedUserRuntimeConfig(
        forced_model=llm.model if llm is not None else None,
        forced_reasoning_effort=llm.reasoning_effort if llm is not None else None,
        max_agent_turns=min(global_max_turns, user_max, request_max),
        allow_mutations=(
            global_allow_mutations
            and not (execution is not None and execution.allow_mutations is False)
            and request_allow_mutations is not False
        ),
    )


def _validate_text_lists(*groups: List[str]) -> None:
    for value in (item for group in groups for item in group):
        if not isinstance(value, str):
            raise ValueError("preference entries must be strings")
        cleaned = " ".join(value.split())
        if not cleaned or len(cleaned) > 200:
            raise ValueError("preference entries must contain 1 to 200 characters")
