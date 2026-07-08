"""Google Calendar tool specs + LangChain tool wrappers.

Mirrors ``tools/todoist/tools.py``: ``get_calendar_tool_specs(client)`` pairs each
schema with the matching client method (and the mutation flag) into ``ToolSpec``s
for the registry; ``build_calendar_langchain_tools(dispatch)`` builds the
``ToolNode`` wrappers that route through the shared dispatch pipeline (mutation
guard, result envelope, tracing).
"""

from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import InjectedToolCallId, tool

from agents.agent_api.app.tools.base import DispatchFn, ToolSpec
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
- Timed events need BOTH start_datetime and end_datetime. If the user gives only a start, infer a duration (default 1h; "coffee" ~30min, "dinner" ~2h).
- All-day events use start_date/end_date; end is exclusive (a 1-day event on Jul 2 → start_date=2026-07-02, end_date=2026-07-03).
- calendar_id defaults to "primary" — pass it only when the user names another calendar.
- Before creating a timed event, call get_freebusy for that slot and warn of conflicts. Do not silently double-book.
- Deleting an event (`delete_calendar_event`) is system-gated like every delete: just issue the call and let the approval prompt handle confirmation — do NOT add your own "are you sure?". Calendar creates/updates count toward the same 5+ mutations-per-turn bulk gate.
- Recurring events use RRULE strings in the recurrence array (e.g. ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=10"]).
- Attendees are email addresses. If the user gives a name without an email, ask for it.
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
    return [
        ToolSpec(
            name=name,
            openai_schema=schemas[name],
            handler=handler,
            mutating=name in MUTATING_CALENDAR_TOOLS,
        )
        for name, handler in handlers.items()
    ]


def build_calendar_langchain_tools(dispatch: DispatchFn) -> List[Any]:
    """Build LangChain tool wrappers that delegate to the shared dispatch pipeline."""

    @tool
    def list_calendars(tool_call_id: Annotated[str, InjectedToolCallId]) -> Dict[str, Any]:
        """List the user's Google Calendars."""

        return dispatch(tool_call_id, "list_calendars", {})

    @tool
    def list_calendar_events(
        time_min: str,
        time_max: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        calendar_id: Optional[str] = None,
        max_results: Optional[int] = None,
        q: Optional[str] = None,
        single_events: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """List Google Calendar events in a time range."""

        return dispatch(
            tool_call_id,
            "list_calendar_events",
            {
                "time_min": time_min,
                "time_max": time_max,
                "calendar_id": calendar_id,
                "max_results": max_results,
                "q": q,
                "single_events": single_events,
            },
        )

    @tool
    def get_calendar_event(
        event_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        calendar_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch one Google Calendar event by id."""

        return dispatch(
            tool_call_id,
            "get_calendar_event",
            {"event_id": event_id, "calendar_id": calendar_id},
        )

    @tool
    def create_calendar_event(
        summary: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        start_datetime: Optional[str] = None,
        end_datetime: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        timezone: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        recurrence: Optional[List[str]] = None,
        reminders_minutes: Optional[List[int]] = None,
        calendar_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Google Calendar event."""

        return dispatch(
            tool_call_id,
            "create_calendar_event",
            {
                "summary": summary,
                "start_datetime": start_datetime,
                "end_datetime": end_datetime,
                "start_date": start_date,
                "end_date": end_date,
                "description": description,
                "location": location,
                "timezone": timezone,
                "attendees": attendees,
                "recurrence": recurrence,
                "reminders_minutes": reminders_minutes,
                "calendar_id": calendar_id,
            },
        )

    @tool
    def update_calendar_event(
        event_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        summary: Optional[str] = None,
        start_datetime: Optional[str] = None,
        end_datetime: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        timezone: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        recurrence: Optional[List[str]] = None,
        reminders_minutes: Optional[List[int]] = None,
        calendar_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update (patch) an existing Google Calendar event."""

        return dispatch(
            tool_call_id,
            "update_calendar_event",
            {
                "event_id": event_id,
                "summary": summary,
                "start_datetime": start_datetime,
                "end_datetime": end_datetime,
                "start_date": start_date,
                "end_date": end_date,
                "description": description,
                "location": location,
                "timezone": timezone,
                "attendees": attendees,
                "recurrence": recurrence,
                "reminders_minutes": reminders_minutes,
                "calendar_id": calendar_id,
            },
        )

    @tool
    def delete_calendar_event(
        event_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        calendar_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Delete a Google Calendar event by id."""

        return dispatch(
            tool_call_id,
            "delete_calendar_event",
            {"event_id": event_id, "calendar_id": calendar_id},
        )

    @tool
    def get_freebusy(
        time_min: str,
        time_max: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        calendar_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Check free/busy status for a time range."""

        return dispatch(
            tool_call_id,
            "get_freebusy",
            {"time_min": time_min, "time_max": time_max, "calendar_ids": calendar_ids},
        )

    return [
        list_calendars,
        list_calendar_events,
        get_calendar_event,
        create_calendar_event,
        update_calendar_event,
        delete_calendar_event,
        get_freebusy,
    ]


__all__ = [
    "CALENDAR_GROUNDING_NOTE",
    "CALENDAR_PROMPT_FRAGMENT",
    "build_calendar_langchain_tools",
    "get_calendar_tool_specs",
]
