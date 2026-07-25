"""Shared builders for runtime-context test fixtures.

Keeps the RuntimeContextSnapshot construction (preferences, domains, tool names)
in one place so prompt/registry tests describe only what differs.
"""

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1
from agents.agent_api.app.user_context.runtime import (
    DomainAvailability,
    RuntimeContextSnapshot,
)

_CAPABILITIES: Dict[str, List[str]] = {
    "todoist": ["tasks", "projects", "comments"],
    "google_calendar": ["calendar_events", "free_busy"],
}
_TOOL_NAMES: Dict[str, List[str]] = {
    "todoist": ["add_todoist_task", "get_tasks"],
    "google_calendar": ["list_calendar_events", "delete_calendar_event"],
}


def make_preferences(
    *,
    task_provider: str = "todoist",
    event_provider: str = "todoist",
    calendar_usage: Optional[str] = None,
    category_defaults: Optional[Dict[str, str]] = None,
    reminder_provider: Optional[str] = None,
    time_related_provider: Optional[str] = None,
    explicit_calendar_provider: Optional[str] = None,
    routing_exceptions: Optional[List[Dict[str, str]]] = None,
    communication: Optional[Dict[str, object]] = None,
    fallback_calendar: Optional[str] = None,
    access: Optional[Dict[str, object]] = None,
    onboarding: Optional[Dict[str, object]] = None,
) -> AssistantPreferencesV1:
    routing = {
        "task_provider": task_provider,
        "event_provider": event_provider,
        "calendar_usage": calendar_usage
        or ("default" if event_provider == "google_calendar" else "explicit_only"),
    }
    if reminder_provider is not None:
        routing["reminder_provider"] = reminder_provider
    if time_related_provider is not None:
        routing["time_related_provider"] = time_related_provider
    if explicit_calendar_provider is not None:
        routing["explicit_calendar_provider"] = explicit_calendar_provider
    if routing_exceptions is not None:
        routing["exceptions"] = routing_exceptions
    calendar = {"event_category_defaults": category_defaults or {}}
    if fallback_calendar is not None:
        calendar["fallback_calendar"] = fallback_calendar
    return AssistantPreferencesV1.model_validate(
        {
            "communication": communication
            or {"tone": "casual", "verbosity": "concise"},
            "routing": routing,
            "domains": {
                "todoist": {},
                "google_calendar": calendar,
            },
            **({"access": access} if access is not None else {}),
            **({"onboarding": onboarding} if onboarding is not None else {}),
        }
    )


def make_snapshot(
    *,
    display_name: str = "Tester",
    active: Iterable[str] = ("todoist", "google_calendar"),
    unavailable: Optional[Dict[str, str]] = None,
    registered_tools: Optional[List[str]] = None,
    preferences: Optional[AssistantPreferencesV1] = None,
    timezone_name: str = "Asia/Singapore",
    locale: str = "en",
) -> RuntimeContextSnapshot:
    active = set(active)
    unavailable = unavailable or {}
    domains: List[DomainAvailability] = []
    for provider in ("todoist", "google_calendar"):
        if provider in active:
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="active",
                    connection_id=f"{provider}-conn",
                    capabilities=_CAPABILITIES[provider],
                    tool_names=_TOOL_NAMES[provider],
                )
            )
        elif provider in unavailable:
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="unavailable",
                    reason=unavailable[provider],
                    connection_id=f"{provider}-conn",
                    capabilities=_CAPABILITIES[provider],
                )
            )
    if registered_tools is None:
        registered_tools = ["ask_user"] + [
            name
            for provider in ("todoist", "google_calendar")
            if provider in active
            for name in _TOOL_NAMES[provider]
        ]
    return RuntimeContextSnapshot(
        user_id="user-id",
        display_name=display_name,
        timezone=timezone_name,
        locale=locale,
        preference_schema_version=1,
        preference_revision=1,
        preferences=preferences or make_preferences(),
        domains=domains,
        registered_tools=registered_tools,
        resolved_at=datetime.now(timezone.utc),
    )
