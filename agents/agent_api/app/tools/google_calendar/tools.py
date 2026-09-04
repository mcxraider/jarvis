"""Google Calendar tool specs and prompt contributions.

Mirrors ``tools/todoist/tools.py``: ``get_calendar_tool_specs(client)`` pairs each
schema with the matching client method (and the mutation flag) into ``ToolSpec``s
for the registry.
"""

import inspect
from typing import Any, Dict, List, Optional

from agents.agent_api.app.tools.base import ToolSpec
from agents.agent_api.app.tools.google_calendar.schemas import (
    MUTATING_CALENDAR_TOOLS,
    get_calendar_tool_schemas,
)


# --- Prompt contributions -----------------------------------------------------
# See tools/todoist/tools.py for the rationale: the domain owns its prompt text,
# wired onto the Google Calendar DomainAdapter and emitted only when the domain is
# active for this user. Kept self-contained (no cross-domain references) so the
# fragment reads correctly even when Todoist is unavailable.

CALENDAR_GROUNDING_NOTE = (
    "Google Calendar: never invent an `event_id` — fetch events "
    "(`list_calendar_events` / `get_calendar_event`) first, then update or delete "
    "by a returned id."
)

CALENDAR_PROMPT_FRAGMENT = """\
## Google Calendar tool tips
- All datetimes use RFC 3339 with timezone offset (e.g. 2026-07-02T14:00:00+08:00). Resolve relative dates to concrete ISO first, using the user's timezone from Runtime context.
- Timed events need BOTH start_datetime and end_datetime. If the user gives only a start, infer a duration (default 1h; "coffee" ~30min, "dinner" ~2h). If similar past events exist, prefer their duration over the generic default.
- All-day events use start_date/end_date; end is exclusive (a 1-day event on Jul 2 → start_date=2026-07-02, end_date=2026-07-03).
- calendar_id defaults to "primary" — pass it only when the user names another calendar.
- Before creating a timed event, call get_freebusy for that slot and warn of conflicts. Do not silently double-book.
- Keep reads bounded: always pass explicit time_min/time_max and use a small default window (e.g. next 30 days) when the user doesn't state one. Collection reads return one page of 50 items by default; pass a returned next_page_token verbatim to page within that window before widening it.
- For recurring events, don't infer scope from a single occurrence — read the master series/recurrence_id first, then set update_scope to this_instance, entire_series, or this_and_following as appropriate. Recurrence uses RRULE strings (e.g. ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=10"]).
- On updates, preserve title, attendees, location, meeting link, and notes unless the user asked to change them — don't drop fields silently.
- Treat deletes and any broad availability/reminder changes as high-impact: restate the exact event(s) and diff before writing, don't just execute.
- Reminders: use the structured reminders object (use_default + overrides with method/minutes), not free-form text.
- Temporary holds: default to transparent (non-blocking) unless the user wants a blocking focus block.
- There's no reliable global room search — build a candidate room list from past meetings/locations/resource attendees, then check availability on that set.
"- A person's name in an event title does not make them an attendee. Use only the user's calendars for availability and create the event without attendees unless the user explicitly asks to invite or add someone as an attendee. Only then search bounded recent events for their email and ask if it cannot be found.
- Calendar creates and updates count toward the shared 5+ mutations-per-turn bulk gate.
- When listing events, keep single_events=true so recurrences expand into instances."""

def get_calendar_tool_specs(calendar_client: Any) -> List[ToolSpec]:
    """Build one :class:`ToolSpec` per Calendar tool (schema + handler + mutating)."""

    schemas = {schema["function"]["name"]: schema for schema in get_calendar_tool_schemas()}
    handlers = {
        "list_calendars": calendar_client.list_calendars,
        "list_calendar_events": calendar_client.list_calendar_events,
        "get_calendar_event": calendar_client.get_calendar_event,
        "create_calendar_event": calendar_client.create_calendar_event,
        "update_calendar_event": calendar_client.update_calendar_event,
        "delete_calendar_event": calendar_client.delete_calendar_event,
        "get_freebusy": calendar_client.get_freebusy,
    }
    async_handlers = {
        name: candidate
        for name in handlers
        if inspect.iscoroutinefunction(
            candidate := getattr(calendar_client, f"async_{name}", None)
        )
    }
    return [
        ToolSpec(
            name=name,
            openai_schema=schemas[name],
            handler=handler,
            mutating=name in MUTATING_CALENDAR_TOOLS,
            async_handler=async_handlers.get(name),
        )
        for name, handler in handlers.items()
    ]


__all__ = [
    "CALENDAR_GROUNDING_NOTE",
    "CALENDAR_PROMPT_FRAGMENT",
    "get_calendar_tool_specs",
]
