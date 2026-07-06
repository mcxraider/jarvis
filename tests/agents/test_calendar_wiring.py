"""Stage 4: calendar domain wired into the live runtime.

Stages 1-3 built the calendar domain in isolation; these tests assert the four
integration seams that make it actually run:

1. registry     — calendar tools register iff a client is supplied
2. confirm gate — calendar mutations classify correctly (delete gated, else low)
3. prompt       — one combined prompt always carries both Todoist and Calendar
4. selector     — keyword routes for an unregistered domain are dropped, never surfaced

These are the seams a future domain (Gmail, Notion) reuses, so a regression here
would silently un-gate a destructive tool or leak tools the user can't call.
"""

from unittest.mock import MagicMock

from agents.agent_api.app.graph import risk
from agents.agent_api.app.graph.prompts.orchestrator import get_orchestrator_prompt
from agents.agent_api.app.tools.metadata import get_meta
from agents.agent_api.app.tools.registry_factory import build_registry_from_clients
from agents.agent_api.app.tools.selectors.keyword import KeywordToolSelector
from tests.agents.runtime_helpers import make_snapshot

_CALENDAR_TOOLS = {
    "list_calendars",
    "list_calendar_events",
    "get_calendar_event",
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
    "get_freebusy",
}


def _tool_call(name):
    return {"function": {"name": name, "arguments": "{}"}}


# -- 1. dynamic registry ------------------------------------------------------


def test_calendar_registered_when_client_supplied():
    registry = build_registry_from_clients(MagicMock(), calendar_client=MagicMock())
    names = {spec.name for spec in registry.specs}
    assert _CALENDAR_TOOLS <= names
    # Todoist and control tools still present alongside calendar.
    assert "add_todoist_task" in names
    assert "ask_user" in names


def test_calendar_absent_without_client():
    registry = build_registry_from_clients(MagicMock())  # no calendar_client
    names = {spec.name for spec in registry.specs}
    assert names.isdisjoint(_CALENDAR_TOOLS)
    assert "add_todoist_task" in names  # Todoist unaffected


def test_calendar_can_register_without_todoist():
    registry = build_registry_from_clients(
        todoist_client=None,
        calendar_client=MagicMock(),
    )
    names = {spec.name for spec in registry.specs}
    assert _CALENDAR_TOOLS <= names
    assert "add_todoist_task" not in names
    assert "ask_user" in names


# -- 2. confirm/risk gate -----------------------------------------------------


def test_calendar_delete_routes_to_confirm_gate():
    # Safety-critical: a single delete must be "risky" (confirm gate), not "low".
    assert risk.classify_risk(_tool_call("delete_calendar_event"), {"tool_results": []}) == "risky"
    assert "delete_calendar_event" in risk.RISKY_TOOLS
    assert get_meta("delete_calendar_event").irreversible is True


def test_calendar_single_mutation_is_low():
    for name in ("create_calendar_event", "update_calendar_event"):
        assert risk.classify_risk(_tool_call(name), {"tool_results": []}) == "low"


def test_calendar_reads_are_not_gated():
    for name in ("list_calendar_events", "get_calendar_event", "get_freebusy", "list_calendars"):
        assert risk.classify_risk(_tool_call(name), {"tool_results": []}) == "read"


def test_calendar_bulk_crosses_threshold():
    # Enough prior mutations that the next calendar mutation trips the bulk gate.
    state = {"tool_results": [{"tool_name": "create_calendar_event"}] * risk.BULK_THRESHOLD}
    assert risk.classify_risk(_tool_call("create_calendar_event"), state) == "risky"


# -- 3. dynamic prompt --------------------------------------------------------


def test_prompt_includes_both_domains_when_both_active():
    # With both domains active in the runtime context, both fragments appear and
    # the "Available tools" line advertises the registered tools of each.
    snapshot = make_snapshot(active=("todoist", "google_calendar"))
    prompt = get_orchestrator_prompt(runtime_context=snapshot)
    assert "Google Calendar tool tips" in prompt
    assert "Todoist tool tips" in prompt
    assert "list_calendar_events" in prompt  # calendar tool in the tools line
    assert "add_todoist_task" in prompt  # todoist tool in the tools line
    # The calendar block must teach that calendar deletes are system-gated,
    # else the model double-confirms them (worse UX than task deletes).
    assert "`delete_calendar_event`) is system-gated" in prompt


def test_prompt_has_no_router_section():
    # Regression guard: the old per-domain "## Routing" dispatch section is gone —
    # domain framing now lives in the role line, not a separate router block.
    prompt = get_orchestrator_prompt(runtime_context=make_snapshot())
    assert "## Routing" not in prompt


# -- 4. selector safety -------------------------------------------------------


def test_selector_drops_unregistered_calendar_tool():
    # Todoist-only registry: a calendar keyword route must not surface calendar
    # tools the registry doesn't contain.
    registry = build_registry_from_clients(MagicMock())  # no calendar
    selector = KeywordToolSelector(allow_mutations=True)
    schemas = selector.select_schemas("schedule a meeting tomorrow at 2pm", registry)
    surfaced = {s["function"]["name"] for s in schemas}
    assert surfaced.isdisjoint(_CALENDAR_TOOLS)


def test_selector_surfaces_calendar_tool_when_registered():
    registry = build_registry_from_clients(MagicMock(), calendar_client=MagicMock())
    selector = KeywordToolSelector(allow_mutations=True)
    schemas = selector.select_schemas("what's on my calendar today", registry)
    surfaced = {s["function"]["name"] for s in schemas}
    assert "list_calendar_events" in surfaced
