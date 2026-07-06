"""Runtime user profiles drive registry and prompt capability claims."""

from datetime import datetime, timezone

from agents.agent_api.app.credentials import IntegrationCredential
from agents.agent_api.app.graph.prompts.orchestrator import get_orchestrator_prompt
from agents.agent_api.app.tools.registry_factory import (
    apply_registered_tools,
    build_runtime_registry,
)
from agents.agent_api.app.tracing import NULL_TRACE
from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1
from agents.agent_api.app.user_context.runtime import (
    DomainAvailability,
    ResolvedRuntimeContext,
    RuntimeContextSnapshot,
)


def _context(
    *,
    name: str,
    event_provider: str,
    calendar_status: str = "active",
    calendar_reason: str | None = None,
    category_defaults: dict | None = None,
) -> ResolvedRuntimeContext:
    preferences = AssistantPreferencesV1.model_validate(
        {
            "communication": {"tone": "casual", "verbosity": "concise"},
            "routing": {
                "task_provider": "todoist",
                "event_provider": event_provider,
                "calendar_usage": (
                    "explicit_only" if event_provider == "todoist" else "default"
                ),
            },
            "domains": {
                "todoist": {},
                "google_calendar": {
                    "event_category_defaults": category_defaults or {}
                },
            },
        }
    )
    domains = [
        DomainAvailability(
            provider="todoist",
            status="active",
            connection_id="todoist-connection",
            capabilities=["tasks"],
        ),
        DomainAvailability(
            provider="google_calendar",
            status=calendar_status,
            reason=calendar_reason,
            connection_id="calendar-connection",
            capabilities=["calendar_events"],
        ),
    ]
    credentials = {
        "todoist": IntegrationCredential(
            connection_id="todoist-connection",
            provider="todoist",
            secret="todoist-secret",
        )
    }
    if calendar_status == "active":
        credentials["google_calendar"] = IntegrationCredential(
            connection_id="calendar-connection",
            provider="google_calendar",
            secret='{"token": "calendar-secret"}',
        )
    snapshot = RuntimeContextSnapshot(
        user_id="user-id",
        display_name=name,
        timezone="Asia/Singapore",
        locale="en",
        preference_schema_version=1,
        preference_revision=1,
        preferences=preferences,
        domains=domains,
        resolved_at=datetime.now(timezone.utc),
    )
    return ResolvedRuntimeContext(snapshot=snapshot, credentials=credentials)


def _build(context):
    """Build the runtime registry and record tool names on the snapshot."""

    registry, clients, tool_names = build_runtime_registry(context, NULL_TRACE)
    apply_registered_tools(context, registry, tool_names)
    return registry, clients


def test_runtime_registry_exposes_only_active_domains():
    context = _context(
        name="Jerry",
        event_provider="todoist",
        calendar_status="unavailable",
        calendar_reason="disabled",
    )

    registry, _clients = _build(context)
    names = {spec.name for spec in registry.specs}

    assert "add_todoist_task" in names
    assert "list_calendar_events" not in names
    assert set(context.snapshot.registered_tools) == names


def test_prompt_reports_unavailable_domain_without_calendar_instructions():
    context = _context(
        name="Jerry",
        event_provider="todoist",
        calendar_status="unavailable",
        calendar_reason="not_connected",
    )
    _build(context)

    prompt = get_orchestrator_prompt(runtime_context=context.snapshot)

    assert "- Google Calendar is unavailable because it is not connected" in prompt
    assert "## Google Calendar tool tips" not in prompt
    assert "Event provider: todoist" in prompt
    assert "Calendar usage: explicit_only" in prompt


def test_zachary_calendar_mapping_is_rendered_from_preferences():
    context = _context(
        name="Zachary",
        event_provider="google_calendar",
        category_defaults={
            "social": "Personal (Zac Kam)",
            "work": "Work (Zac Kam)",
            "classes": "NUS Schedule",
        },
    )
    _build(context)

    prompt = get_orchestrator_prompt(runtime_context=context.snapshot)

    assert "Zachary's personal assistant" in prompt
    assert "Event provider: google_calendar" in prompt
    assert "social → Personal (Zac Kam)" in prompt
    assert "classes → NUS Schedule" in prompt


def test_runtime_snapshot_never_serializes_credentials():
    context = _context(name="Jerry", event_provider="todoist")
    _build(context)

    serialized = context.snapshot.model_dump_json()

    assert "todoist-secret" not in serialized
    assert "calendar-secret" not in serialized
