"""Secret-free runtime capability snapshots and v1 resume compatibility."""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1
from agents.agent_api.app.user_context.policy import (
    ResourceRestrictions,
    RuntimePolicy,
)


class RuntimeContextError(RuntimeError):
    """Runtime user context cannot be resolved safely."""


class PolicyShadowMismatchError(RuntimeContextError):
    """Typed policy and the compatibility-window shadow do not agree."""

    def __init__(self, *, runtime_matches: bool, access_matches: bool) -> None:
        super().__init__("Stored user policy shadow comparison failed.")
        self.runtime_matches = runtime_matches
        self.access_matches = access_matches


class DomainAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    status: Literal["active", "unavailable", "unsupported"]
    reason: Optional[str] = None
    connection_id: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    tool_names: List[str] = Field(default_factory=list)


class _SnapshotMethods:
    domains: List[DomainAvailability]

    def active_providers(self) -> set[str]:
        return {
            domain.provider
            for domain in self.domains
            if domain.status == "active"
        }


class LegacyRuntimeContextSnapshot(_SnapshotMethods, BaseModel):
    """Snapshot v1 retained solely for interrupted-thread resumes and fixtures."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    user_id: str
    display_name: str
    timezone: str
    locale: str
    preference_schema_version: int
    preference_revision: int
    preferences: AssistantPreferencesV1
    custom_instructions: str = Field(default="", max_length=10_000)
    domains: List[DomainAvailability]
    registered_tools: List[str] = Field(default_factory=list)
    resolved_at: datetime


class RuntimeContextSnapshot(_SnapshotMethods, BaseModel):
    """Snapshot v2 for fresh runs; it contains no legacy preference document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    user_id: str
    display_name: str
    timezone: str
    locale: str
    custom_instructions: str = Field(default="", max_length=10_000)
    runtime_policy: RuntimePolicy = Field(default_factory=RuntimePolicy)
    resource_restrictions: ResourceRestrictions = Field(
        default_factory=ResourceRestrictions
    )
    policy_revision: int = Field(gt=0)
    domains: List[DomainAvailability]
    registered_tools: List[str] = Field(default_factory=list)
    resolved_at: datetime


RuntimeContextSnapshotLike = Union[
    RuntimeContextSnapshot,
    LegacyRuntimeContextSnapshot,
]


def parse_runtime_context_snapshot(payload: object) -> RuntimeContextSnapshotLike:
    """Parse either persisted version without weakening either schema."""

    if not isinstance(payload, dict):
        raise RuntimeContextError("Runtime snapshot must be a JSON object.")
    if payload.get("schema_version", 1) == 1:
        return LegacyRuntimeContextSnapshot.model_validate(payload)
    return RuntimeContextSnapshot.model_validate(payload)


def runtime_policy_from_snapshot(
    snapshot: RuntimeContextSnapshotLike,
) -> RuntimePolicy:
    if isinstance(snapshot, RuntimeContextSnapshot):
        return snapshot.runtime_policy
    return RuntimePolicy(
        forced_model=snapshot.preferences.llm.model,
        forced_reasoning_effort=snapshot.preferences.llm.reasoning_effort,
        max_agent_turns=snapshot.preferences.execution.max_agent_turns,
        allow_mutations=snapshot.preferences.execution.allow_mutations,
    )


def resource_restrictions_from_snapshot(
    snapshot: RuntimeContextSnapshotLike,
) -> ResourceRestrictions:
    if isinstance(snapshot, RuntimeContextSnapshot):
        return snapshot.resource_restrictions
    return ResourceRestrictions.model_validate(
        snapshot.preferences.access.model_dump(mode="json")
    )


def policy_revision_from_snapshot(snapshot: RuntimeContextSnapshotLike) -> int:
    if isinstance(snapshot, RuntimeContextSnapshot):
        return snapshot.policy_revision
    return snapshot.preference_revision


@dataclass(frozen=True)
class ResolvedRuntimeContext:
    snapshot: RuntimeContextSnapshotLike
    credentials: Dict[str, IntegrationCredential]
