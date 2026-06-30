# Google Calendar Integration Plan

## Context

Jarvis is Jerry's single-user Telegram assistant backed by a Python LangGraph agent. The agent currently has Todoist tools for task management. This plan adds Google Calendar as a second tool domain so Jerry can manage his schedule through the same conversational interface — list events, check availability, create/update/delete events.

The existing architecture (`ToolSpec` + `ToolRegistry` + `ToolDispatcher`) makes adding domains straightforward: create schemas, client, and tools files; register in the factory; update the prompt and selector.

The primary complexity is **OAuth auth** — unlike Todoist (API key), Calendar needs OAuth 2.0 with token refresh. Since Jarvis runs headlessly as a FastAPI server, initial consent must happen once via a setup script, then the runtime uses persisted refresh tokens.

---

## MVP Tool Set (7 tools)

| Tool | Mutating | Why |
|------|----------|-----|
| `list_calendars` | No | Discover which calendars exist |
| `list_calendar_events` | No | "What's on my calendar today?" — highest-frequency query |
| `get_calendar_event` | No | Fetch details / ground an ID before mutation |
| `create_calendar_event` | **Yes** | Schedule meetings and appointments |
| `update_calendar_event` | **Yes** | Reschedule, add attendees, change details |
| `delete_calendar_event` | **Yes** | Cancel events |
| `get_freebusy` | No | "When am I free?" / conflict detection before booking |

Deferred: `search_events`, `respond_to_event`, `create_events` (bulk), `list_colors`.

---

## File Structure

### New files (mirror Todoist domain pattern)

```
agents/agent_api/app/tools/calendar/
    __init__.py          # re-exports get_calendar_tool_specs, build_calendar_langchain_tools
    auth.py              # OAuth credential loading + token refresh
    client.py            # GoogleCalendarClient (wraps discovery service)
    schemas.py           # OpenAI function schemas + MUTATING_CALENDAR_TOOLS set
    tools.py             # get_calendar_tool_specs() + build_calendar_langchain_tools(dispatch)

scripts/
    google_calendar_oauth_setup.py   # One-time interactive consent script
```

### Files to modify

| File | Change |
|------|--------|
| `agents/agent_api/app/tools/registry_factory.py` | Add `registry.register(get_calendar_tool_specs(cal_client), build_calendar_langchain_tools)` |
| `agents/agent_api/app/tools/selectors/keyword.py` | Add calendar keyword routes |
| `agents/agent_api/app/graph/prompts/orchestrator.py` | Add Calendar-specific instructions |
| `agents/agent_api/app/graph/builder.py` | Instantiate `GoogleCalendarClient` in `run_jarvis()` |
| `agents/agent_api/app/tools/dispatcher.py` | Catch `GoogleCalendarApiError` alongside `TodoistApiError` |
| `requirements.txt` | Add google-api-python-client, google-auth, google-auth-oauthlib, google-auth-httplib2 |
| `.env.sample` | Document `GOOGLE_CALENDAR_TOKEN_JSON` / `GOOGLE_CALENDAR_CREDENTIALS_PATH` |

---

## Auth Strategy

### One-time setup (interactive, local)

`scripts/google_calendar_oauth_setup.py`:
- Uses `InstalledAppFlow.from_client_secrets_file(path, SCOPES)` with `run_local_server(port=0)`
- Produces a token JSON blob (access token + refresh token)
- Output: print base64-encoded token for pasting into env var, or write to `token.json`

### Runtime (`auth.py`)

```python
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_calendar_credentials() -> Credentials:
    # 1. Load from env var GOOGLE_CALENDAR_TOKEN_JSON (base64-encoded JSON)
    # 2. Fallback: load from file path GOOGLE_CALENDAR_CREDENTIALS_PATH
    # 3. If expired + has refresh_token: refresh automatically
    # 4. Persist refreshed token back to env source
    # Raises GoogleCalendarApiError(kind="auth") if no valid creds
```

The Google Auth library auto-refreshes tokens when passed to `build()`, so the client doesn't need explicit refresh logic beyond initial load.

### Env vars to add

```
GOOGLE_CALENDAR_TOKEN_JSON=        # base64 of token.json contents (preferred for Docker)
GOOGLE_CALENDAR_CREDENTIALS_PATH=  # alternative: path to token.json file
```

---

## Client Design

`tools/calendar/client.py` — follows TodoistApiClient patterns:

```python
class GoogleCalendarClient:
    def __init__(self, tracer=None, telegram_user_id=None):
        self._service = None  # lazy-built from credentials

    def list_calendars(self, arguments: Dict) -> Dict
    def list_calendar_events(self, arguments: Dict) -> Dict
    def get_calendar_event(self, arguments: Dict) -> Dict
    def create_calendar_event(self, arguments: Dict) -> Dict
    def update_calendar_event(self, arguments: Dict) -> Dict
    def delete_calendar_event(self, arguments: Dict) -> Dict
    def get_freebusy(self, arguments: Dict) -> Dict
```

Key patterns:
- Lazy `service` property (builds on first use)
- Shared `_execute(request, operation)` wrapper with retry (429, 500, 503), error classification, tracing
- `GoogleCalendarApiError` with `kind` field: auth, not-found, rate-limit, transient, validation
- Returns cleaned dicts (strip internal metadata, normalize datetimes)

### Response normalization

List responses should return simplified event objects:
```python
{
    "event_id": "abc123",
    "summary": "Team standup",
    "start": "2026-07-02T09:00:00+08:00",
    "end": "2026-07-02T09:30:00+08:00",
    "location": "Zoom",
    "attendees": ["alice@co.com"],
    "status": "confirmed"
}
```
This keeps LLM context tokens low (the raw Google API response is verbose with ~40 fields).

---

## Schema Design (key examples)

### `create_calendar_event`
```json
{
  "type": "function",
  "function": {
    "name": "create_calendar_event",
    "description": "Create a Google Calendar event.",
    "parameters": {
      "type": "object",
      "properties": {
        "summary": {"type": "string", "description": "Event title."},
        "start_datetime": {"type": "string", "description": "Start as RFC 3339 with tz offset (e.g. 2026-07-02T14:00:00+08:00). Required for timed events."},
        "end_datetime": {"type": "string", "description": "End as RFC 3339. Required for timed events."},
        "start_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "Start date for all-day events (YYYY-MM-DD)."},
        "end_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "End date (exclusive) for all-day events."},
        "description": {"type": "string"},
        "location": {"type": "string"},
        "timezone": {"type": "string", "description": "IANA timezone (e.g. 'Asia/Taipei'). Defaults to user timezone."},
        "attendees": {"type": "array", "items": {"type": "string"}, "description": "Email addresses."},
        "recurrence": {"type": "array", "items": {"type": "string"}, "description": "RRULE strings, e.g. ['RRULE:FREQ=WEEKLY;BYDAY=MO']."},
        "reminders_minutes": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "calendar_id": {"type": "string", "description": "Target calendar. Defaults to 'primary'."}
      },
      "required": ["summary"],
      "additionalProperties": false
    }
  }
}
```

### `list_calendar_events`
```json
{
  "type": "function",
  "function": {
    "name": "list_calendar_events",
    "description": "List Google Calendar events in a time range.",
    "parameters": {
      "type": "object",
      "properties": {
        "time_min": {"type": "string", "description": "Start of range, RFC 3339 with tz offset."},
        "time_max": {"type": "string", "description": "End of range, RFC 3339 with tz offset."},
        "calendar_id": {"type": "string", "description": "Defaults to 'primary'."},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 250},
        "q": {"type": "string", "description": "Free-text search."},
        "single_events": {"type": "boolean", "description": "Expand recurring events. Default true."}
      },
      "required": ["time_min", "time_max"],
      "additionalProperties": false
    }
  }
}
```

### `get_freebusy`
```json
{
  "type": "function",
  "function": {
    "name": "get_freebusy",
    "description": "Check free/busy status for a time range to detect scheduling conflicts.",
    "parameters": {
      "type": "object",
      "properties": {
        "time_min": {"type": "string", "description": "Start of window, RFC 3339."},
        "time_max": {"type": "string", "description": "End of window, RFC 3339."},
        "calendar_ids": {"type": "array", "items": {"type": "string"}, "description": "Calendars to check. Defaults to ['primary']."}
      },
      "required": ["time_min", "time_max"],
      "additionalProperties": false
    }
  }
}
```

---

## System Prompt Changes

Add to `orchestrator.py` after the Todoist section:

```
## Google Calendar tool tips
- All datetimes use RFC 3339 with timezone offset (e.g. 2026-07-02T14:00:00+08:00). Resolve relative dates to concrete ISO, then format. Use the user's timezone from Runtime context.
- For timed events: both start_datetime and end_datetime are required. If the user gives only a start time, infer 1-hour duration (or contextual: "coffee" = 30min, "dinner" = 2h).
- For all-day events: use start_date/end_date. End is exclusive (1-day event on Jul 2 → start_date="2026-07-02", end_date="2026-07-03").
- calendar_id defaults to "primary" — only pass when user specifies a non-default calendar.
- Before creating a timed event, call get_freebusy for that slot and warn of conflicts. Do not silently double-book.
- Grounding rule applies: never fabricate event_id. Fetch events first, then mutate.
- Recurring: pass RRULE strings in recurrence array (e.g. ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=10"]).
- Attendees are email addresses. If user says a name without email, ask for it.
- When listing events, default single_events=true to expand recurrences.
```

Update routing instruction:
```
Todoist is Jerry's app for tasks, to-dos, and reminders. Google Calendar is Jerry's calendar for events and meetings. Route accordingly. If a request spans both ("add a meeting and a prep task"), use both domains.
```

---

## Keyword Selector Routes

Add to `KEYWORD_ROUTES` (longest-first matching handles multi-word phrases):

```python
"cancel meeting": ["list_calendar_events", "delete_calendar_event"],
"cancel event": ["list_calendar_events", "delete_calendar_event"],
"reschedule meeting": ["list_calendar_events", "update_calendar_event"],
"move meeting": ["list_calendar_events", "update_calendar_event"],
"calendar": ["list_calendar_events", "get_freebusy"],
"schedule": ["list_calendar_events", "create_calendar_event", "get_freebusy"],
"meeting": ["list_calendar_events", "create_calendar_event", "get_freebusy"],
"appointment": ["list_calendar_events", "create_calendar_event", "get_freebusy"],
"event": ["list_calendar_events", "create_calendar_event"],
"free": ["get_freebusy"],
"busy": ["get_freebusy"],
"available": ["get_freebusy"],
"availability": ["get_freebusy"],
```

---

## Dependencies

Add to `requirements.txt`:
```
google-api-python-client==2.149.0
google-auth==2.35.0
google-auth-httplib2==0.2.0
google-auth-oauthlib==1.2.1
```

---

## Implementation Order

1. `requirements.txt` — add packages
2. `scripts/google_calendar_oauth_setup.py` — one-time consent script
3. `tools/calendar/auth.py` — credential loading
4. `tools/calendar/client.py` — API client with all 7 methods
5. `tools/calendar/schemas.py` — tool schemas + MUTATING set
6. `tools/calendar/tools.py` — specs + LangChain builders
7. `tools/calendar/__init__.py` — re-exports
8. `tools/registry_factory.py` — register calendar domain
9. `graph/builder.py` — instantiate GoogleCalendarClient
10. `tools/dispatcher.py` — add GoogleCalendarApiError to catch clause
11. `graph/prompts/orchestrator.py` — prompt additions
12. `tools/selectors/keyword.py` — keyword routes
13. `.env.sample` — document new env vars
14. Tests

---

## Verification

1. **Unit tests**: Schema registration, layer consistency (schema names == spec names == langchain tool names), client method routing with mocked Google service
2. **Auth test**: Run `scripts/google_calendar_oauth_setup.py` locally, verify token is produced
3. **Integration smoke test**: With valid token, call `list_calendars` and `list_calendar_events` for today
4. **End-to-end via Telegram**: Send "what's on my calendar today?" and "schedule a meeting tomorrow at 2pm called 'standup'" — verify the full loop (LLM → tool call → Calendar API → response)
5. **Mutation guard**: Verify `delete_calendar_event` is blocked when `ALLOW_MUTATIONS=false`
6. **Conflict detection**: Ask to create an event at a busy time; confirm LLM calls `get_freebusy` first and warns

---

## Key References

- Google Calendar API Python quickstart: https://developers.google.com/workspace/calendar/api/quickstart/python
- MCP reference for tool ideas: https://github.com/nspady/google-calendar-mcp/blob/main/src/tools/registry.ts
- Existing Todoist tool pattern: `agents/agent_api/app/tools/todoist/` (schemas.py, client.py, tools.py)
- Registry factory: `agents/agent_api/app/tools/registry_factory.py`
- Tool selector: `agents/agent_api/app/tools/selectors/keyword.py`
- System prompt: `agents/agent_api/app/graph/prompts/orchestrator.py`
