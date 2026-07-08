"""Google Calendar tool schemas exposed to the LLM.

OpenAI/DeepSeek-compatible function schemas — the contract the model sees when
deciding whether to call a Calendar function and with what arguments. Mirrors
``tools/todoist/schemas.py`` conventions (explicit JSON Schema,
``additionalProperties: false``, ``MUTATING_*`` set for the gate).

Datetime conventions:
- Timed events: ``start_datetime``/``end_datetime`` as RFC 3339 with tz offset.
- All-day events: ``start_date``/``end_date`` (YYYY-MM-DD); end is exclusive.
- ``calendar_id`` defaults to ``"primary"`` in the client when omitted.
"""

from typing import Any, Dict, List

MUTATING_CALENDAR_TOOLS = {
    "create_calendar_event",
    "update_calendar_event",
    "delete_calendar_event",
}

_RFC3339 = "RFC 3339 with timezone offset, e.g. 2026-07-02T14:00:00+08:00."
_DATE_PATTERN = "^\\d{4}-\\d{2}-\\d{2}$"

# Shared event-body properties reused by create (required summary) and update.
_EVENT_PROPERTIES: Dict[str, Any] = {
    "summary": {"type": "string", "description": "Event title."},
    "start_datetime": {
        "type": "string",
        "description": f"Start of a timed event as {_RFC3339} Required with end_datetime for timed events.",
    },
    "end_datetime": {
        "type": "string",
        "description": f"End of a timed event as {_RFC3339}",
    },
    "start_date": {
        "type": "string",
        "pattern": _DATE_PATTERN,
        "description": "Start date for an all-day event (YYYY-MM-DD).",
    },
    "end_date": {
        "type": "string",
        "pattern": _DATE_PATTERN,
        "description": "End date (exclusive) for an all-day event (YYYY-MM-DD).",
    },
    "description": {"type": "string", "description": "Event details/notes."},
    "location": {"type": "string", "description": "Event location (free text)."},
    "timezone": {
        "type": "string",
        "description": "IANA timezone, e.g. 'Asia/Taipei'. Defaults to the user's timezone.",
    },
    "attendees": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Attendee email addresses.",
    },
    "recurrence": {
        "type": "array",
        "items": {"type": "string"},
        "description": "RRULE strings, e.g. ['RRULE:FREQ=WEEKLY;BYDAY=MO'].",
    },
    "reminders_minutes": {
        "type": "array",
        "items": {"type": "integer", "minimum": 0},
        "description": "Popup reminder offsets in minutes before the event.",
    },
    "calendar_id": {
        "type": "string",
        "description": "Target calendar. Defaults to 'primary'.",
    },
}


def _function(name: str, description: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


def get_calendar_tool_schemas() -> List[Dict[str, Any]]:
    """Return the Google Calendar OpenAI/DeepSeek function tool schemas."""

    list_calendars = _function(
        "list_calendars",
        "List the user's Google Calendars (id, summary, primary flag, timezone).",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )

    list_calendar_events = _function(
        "list_calendar_events",
        "List Google Calendar events in a time range.",
        {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": f"Start of range, {_RFC3339}"},
                "time_max": {"type": "string", "description": f"End of range, {_RFC3339}"},
                "calendar_id": {"type": "string", "description": "Defaults to 'primary'."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 250},
                "q": {"type": "string", "description": "Free-text search over event fields."},
                "single_events": {
                    "type": "boolean",
                    "description": "Expand recurring events into instances. Default true.",
                },
            },
            "required": ["time_min", "time_max"],
            "additionalProperties": False,
        },
    )

    get_calendar_event = _function(
        "get_calendar_event",
        "Fetch one Google Calendar event by id.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event id from a prior list/get."},
                "calendar_id": {"type": "string", "description": "Defaults to 'primary'."},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    )

    create_calendar_event = _function(
        "create_calendar_event",
        "Create a Google Calendar event. Provide start/end_datetime for timed events "
        "or start/end_date for all-day events.",
        {
            "type": "object",
            "properties": dict(_EVENT_PROPERTIES),
            "required": ["summary"],
            "additionalProperties": False,
        },
    )

    update_calendar_event = _function(
        "update_calendar_event",
        "Update (patch) an existing Google Calendar event. Only supplied fields change.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event id from a prior list/get."},
                **_EVENT_PROPERTIES,
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    )

    delete_calendar_event = _function(
        "delete_calendar_event",
        "Delete a Google Calendar event by id. Irreversible; the system gates this "
        "behind a confirmation prompt.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "The event id from a prior list/get."},
                "calendar_id": {"type": "string", "description": "Defaults to 'primary'."},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    )

    get_freebusy = _function(
        "get_freebusy",
        "Check free/busy status for a time range to detect scheduling conflicts.",
        {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": f"Start of window, {_RFC3339}"},
                "time_max": {"type": "string", "description": f"End of window, {_RFC3339}"},
                "calendar_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Calendars to check. Defaults to ['primary'].",
                },
            },
            "required": ["time_min", "time_max"],
            "additionalProperties": False,
        },
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


__all__ = ["MUTATING_CALENDAR_TOOLS", "get_calendar_tool_schemas"]
