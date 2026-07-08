"""Runtime-registered tool domains and their credential-aware client factories.

``DOMAIN_ADAPTERS`` is the single registration point for a tool domain. An adapter
bundles everything the runtime needs to turn an enabled integration connection into
live capability: how to build the client from a resolved secret, which tool specs
and LangChain wrappers it exposes, the prompt text it contributes, and an optional
credential validator. Adding Gmail/Notion/Drive later is one entry here — no edits
to ``builder.py`` or the orchestrator prompt.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents.agent_api.app.credentials import (
    IntegrationCredential,
    update_integration_credential,
)
from agents.agent_api.app.tools.base import LangChainToolBuilder, ToolSpec
from agents.agent_api.app.tools.google_calendar.client import GoogleCalendarClient
from agents.agent_api.app.tools.google_calendar.tools import (
    CALENDAR_GROUNDING_NOTE,
    CALENDAR_PROMPT_FRAGMENT,
    build_calendar_langchain_tools,
    get_calendar_tool_specs,
)
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tools.todoist.tools import (
    TODOIST_GROUNDING_NOTE,
    TODOIST_PROMPT_FRAGMENT,
    build_todoist_langchain_tools,
    get_todoist_tool_specs,
)
from agents.agent_api.app.tracing import TracePrinter


class CredentialValidationError(ValueError):
    """A resolved secret does not have the shape the provider requires."""


@dataclass(frozen=True)
class DomainAdapter:
    """Everything the runtime needs to activate one tool domain, in one place."""

    key: str
    display_name: str
    capabilities: List[str]
    build_client: Callable[[IntegrationCredential, TracePrinter], Any]
    get_tool_specs: Callable[[Any], List[ToolSpec]]
    langchain_builder: LangChainToolBuilder
    prompt_fragment: str
    grounding_note: str
    # Optional shape check for a resolved secret. NOT called on the request hot
    # path — the runtime treats successful Vault resolution as sufficient. Admin
    # tooling (scripts/manage_integrations.py) calls this during import/reconnect.
    credential_validator: Optional[Callable[[IntegrationCredential], None]] = field(
        default=None
    )


def _build_todoist_client(
    credential: IntegrationCredential,
    tracer: TracePrinter,
) -> TodoistApiClient:
    return TodoistApiClient(api_key=credential.secret, tracer=tracer)


def _build_calendar_client(
    credential: IntegrationCredential,
    tracer: TracePrinter,
) -> GoogleCalendarClient:
    return GoogleCalendarClient(
        tracer=tracer,
        credential_json=credential.secret,
        persist_callback=lambda secret, expiry: update_integration_credential(
            credential.connection_id,
            secret,
            expiry,
        ),
    )


def _validate_todoist_secret(credential: IntegrationCredential) -> None:
    """A Todoist secret is a bearer API key: a non-empty opaque string."""

    if not isinstance(credential.secret, str) or not credential.secret.strip():
        raise CredentialValidationError("Todoist API key is empty.")


def _validate_calendar_secret(credential: IntegrationCredential) -> None:
    """A Calendar secret is Google authorized-user JSON with a refresh token."""

    try:
        payload = json.loads(credential.secret)
    except (TypeError, ValueError) as exc:
        raise CredentialValidationError(
            "Google Calendar credential is not valid JSON."
        ) from exc
    if not isinstance(payload, dict) or not payload.get("refresh_token"):
        raise CredentialValidationError(
            "Google Calendar credential is missing a refresh_token."
        )


DOMAIN_ADAPTERS: Dict[str, DomainAdapter] = {
    "todoist": DomainAdapter(
        key="todoist",
        display_name="Todoist",
        capabilities=["tasks", "projects", "comments"],
        build_client=_build_todoist_client,
        get_tool_specs=get_todoist_tool_specs,
        langchain_builder=build_todoist_langchain_tools,
        prompt_fragment=TODOIST_PROMPT_FRAGMENT,
        grounding_note=TODOIST_GROUNDING_NOTE,
        credential_validator=_validate_todoist_secret,
    ),
    "google_calendar": DomainAdapter(
        key="google_calendar",
        display_name="Google Calendar",
        capabilities=["calendar_events", "free_busy"],
        build_client=_build_calendar_client,
        get_tool_specs=get_calendar_tool_specs,
        langchain_builder=build_calendar_langchain_tools,
        prompt_fragment=CALENDAR_PROMPT_FRAGMENT,
        grounding_note=CALENDAR_GROUNDING_NOTE,
        credential_validator=_validate_calendar_secret,
    ),
}


__all__ = ["CredentialValidationError", "DomainAdapter", "DOMAIN_ADAPTERS"]
