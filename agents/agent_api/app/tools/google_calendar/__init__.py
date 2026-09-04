"""Google Calendar tool domain (single-user).

Public surface: schema/spec builders, the API client, and the classified error type.
"""

from agents.agent_api.app.tools.google_calendar.auth import GoogleCalendarApiError
from agents.agent_api.app.tools.google_calendar.client import GoogleCalendarClient
from agents.agent_api.app.tools.google_calendar.schemas import (
    MUTATING_CALENDAR_TOOLS,
    get_calendar_tool_schemas,
)
from agents.agent_api.app.tools.google_calendar.tools import get_calendar_tool_specs

__all__ = [
    "MUTATING_CALENDAR_TOOLS",
    "GoogleCalendarApiError",
    "GoogleCalendarClient",
    "get_calendar_tool_schemas",
    "get_calendar_tool_specs",
]
