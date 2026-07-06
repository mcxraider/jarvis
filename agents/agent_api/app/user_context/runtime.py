"""Secret-free runtime capability snapshots."""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1


class RuntimeContextError(RuntimeError):
    """Runtime user context cannot be resolved safely."""


class DomainAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    status: Literal["active", "unavailable", "unsupported"]
    reason: Optional[str] = None
    connection_id: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    tool_names: List[str] = Field(default_factory=list)


class RuntimeContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    user_id: str
    display_name: str
    timezone: str
    locale: str
    preference_schema_version: int
    preference_revision: int
    preferences: AssistantPreferencesV1
    domains: List[DomainAvailability]
    registered_tools: List[str] = Field(default_factory=list)
    resolved_at: datetime

    def active_providers(self) -> set[str]:
        return {
            domain.provider
            for domain in self.domains
            if domain.status == "active"
        }


@dataclass(frozen=True)
class ResolvedRuntimeContext:
    snapshot: RuntimeContextSnapshot
    credentials: Dict[str, IntegrationCredential]
