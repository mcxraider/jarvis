"""Typed user identity, preference, integration, and runtime context models.

Public surface for the unified context resolver. Callers should import from here
rather than reaching into submodules.
"""

from agents.agent_api.app.user_context.domains import (
    ConnectionRow,
    classify_domains,
)
from agents.agent_api.app.user_context.identity import (
    ResolvedIdentity,
    refresh_identity_profile,
    resolve_active_identity,
)
from agents.agent_api.app.user_context.preferences import (
    AssistantPreferencesV1,
    PreferenceConfigurationError,
    ResolvedUserPreferences,
)
from agents.agent_api.app.user_context.resolver import (
    load_thread_runtime_context,
    resolve_runtime_context,
    store_thread_context,
)
from agents.agent_api.app.user_context.runtime import (
    DomainAvailability,
    ResolvedRuntimeContext,
    RuntimeContextError,
    RuntimeContextSnapshot,
)
from agents.agent_api.app.user_context.secrets import (
    resolve_connection_secret,
    resolve_secret_value,
)

__all__ = [
    "AssistantPreferencesV1",
    "ConnectionRow",
    "DomainAvailability",
    "PreferenceConfigurationError",
    "ResolvedIdentity",
    "ResolvedRuntimeContext",
    "ResolvedUserPreferences",
    "RuntimeContextError",
    "RuntimeContextSnapshot",
    "classify_domains",
    "load_thread_runtime_context",
    "refresh_identity_profile",
    "resolve_active_identity",
    "resolve_connection_secret",
    "resolve_runtime_context",
    "resolve_secret_value",
    "store_thread_context",
]
