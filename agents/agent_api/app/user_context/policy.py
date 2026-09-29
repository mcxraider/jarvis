"""Typed, machine-enforced per-user runtime policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Provider = Literal["todoist", "google_calendar"]
FutureProvider = Literal[
    "github",
    "gmail",
    "google_drive",
    "apple_calendar",
    "notion",
]


class RuntimePolicy(BaseModel):
    """Overrides loaded from ``private.user_runtime_policies``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    forced_model: Optional[str] = Field(default=None, min_length=1, max_length=100)
    forced_reasoning_effort: Optional[
        Literal["off", "none", "low", "medium", "high", "xhigh", "max"]
    ] = None
    max_agent_turns: Optional[int] = Field(default=None, strict=True, gt=0, le=50)
    allow_mutations: Optional[StrictBool] = None


class RestrictedResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=300)
    label: str = Field(min_length=1, max_length=200)
    is_primary: bool = False


class ResourceRestrictions(BaseModel):
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
    def validate_unique_resources(self) -> "ResourceRestrictions":
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
    admin_notes: List[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_metadata(self) -> "OnboardingMetadata":
        if len(self.future_providers) != len(set(self.future_providers)):
            raise ValueError("future_providers contains duplicates")
        for note in self.admin_notes:
            if not isinstance(note, str) or not 1 <= len(note.strip()) <= 200:
                raise ValueError("admin notes must contain 1 to 200 characters")
        return self


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
    policy: Optional[RuntimePolicy] = None,
    request_max_turns: Optional[int],
    request_allow_mutations: Optional[bool],
    # V1 compatibility arguments. Fresh callers pass ``policy`` only.
    llm=None,
    execution=None,
) -> ResolvedUserRuntimeConfig:
    """Combine global settings, typed policy, and request-level tightening."""

    if policy is None:
        policy = RuntimePolicy(
            forced_model=getattr(llm, "model", None),
            forced_reasoning_effort=getattr(llm, "reasoning_effort", None),
            max_agent_turns=getattr(execution, "max_agent_turns", None),
            allow_mutations=getattr(execution, "allow_mutations", None),
        )

    user_max = policy.max_agent_turns or global_max_turns
    request_max = request_max_turns or global_max_turns
    return ResolvedUserRuntimeConfig(
        forced_model=policy.forced_model,
        forced_reasoning_effort=policy.forced_reasoning_effort,
        max_agent_turns=min(global_max_turns, user_max, request_max),
        allow_mutations=(
            global_allow_mutations
            and policy.allow_mutations is not False
            and request_allow_mutations is not False
        ),
    )


__all__ = [
    "ResourceRestrictions",
    "RestrictedResource",
    "OnboardingMetadata",
    "ResolvedUserRuntimeConfig",
    "RuntimePolicy",
    "resolve_user_runtime_config",
]
