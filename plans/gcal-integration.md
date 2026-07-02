# Google Calendar Integration Plan

## Context

Jarvis is Jerry's single-user Telegram assistant backed by a Python LangGraph agent. The agent currently has Todoist tools for task management. This plan adds Google Calendar as a second tool domain so Jerry can manage his schedule through the same conversational interface — list events, check availability, create/update/delete events.

The existing architecture (`ToolSpec` + `ToolRegistry` + `ToolDispatcher`) makes adding domains straightforward: create schemas, client, and tools files; register in the factory; update the prompt and selector.

**This is not a greenfield auth build.** The codebase is already multi-user-aware:

- `agents/agent_api/app/credentials.py` resolves per-user secrets from a Supabase `user_credentials` table (`credential_data` JSONB, keyed by `(user_id, service)`), falling back to env vars.
- `agents/agent_api/app/graph/builder.py::_ensure_user()` upserts a `users` row (`telegram_user_id`, `telegram_username`, `telegram_first_name`) on every run, and `_register_thread` / `_log_usage` already write per-user telemetry.
- `TodoistApiClient(telegram_user_id=...)` already resolves each user's Todoist token DB-first (`get_credential(..., service="todoist")`), env-fallback.
- `get_user_preferences(telegram_user_id)` already feeds the user's `timezone` into the system prompt.

So this plan extends an established pattern to (a) a **second service** and (b) a **second credential type**. The two genuinely new problems are:

1. **OAuth lifecycle** — unlike Todoist's static personal-access token (PAT), Calendar needs OAuth 2.0 with a long-lived refresh token, automatic access-token refresh, and write-back of refreshed tokens to Supabase. App-level client secrets stay in env; only per-user tokens live in Supabase.
2. **Per-user dynamic tools + prompts** — today the registry (`build_default_registry(todoist_client)`) and prompt (`get_orchestrator_prompt(tz)`) are static. They must become **functions of the user's connected-services set** so a Todoist-only user never sees Calendar tools (fewer tokens, no hallucinated tools) and vice versa. See the two new sections below.

Onboarding UX (self-service connector selection in Telegram) is **out of scope for the beta** — for the handful of beta users, Jerry generates each link and stores the resulting credential in Supabase once. The plan designs the schema and code so the future self-service flow drops in without rework.

---

## Architecture & Extensibility (why tools, not nodes)

**The recurring question — "do more integrations mean more graph nodes (a Todoist node, a Calendar node)?" — is answered NO, and adopting per-domain nodes would be a regression.** This section records why, so the design isn't relitigated per integration.

**Nodes model control flow, not domains.** The graph's node set (`agent ↔ tools` loop, the `ask_user` HITL interrupt, `prepare_confirm → confirm → executor` for gated mutations; see `graph/builder.py::create_jarvis_graph`) is organized around *how a turn executes*, not *which service it touches*. Domains live one layer below, as `ToolSpec`s aggregated in a flat `ToolRegistry`. Adding an integration is "register more tools," never "add a node." The builder already promises this in-code: "new tool domains plug in via the registry … neither requires editing this function."

**A per-domain router would be strictly worse.** A single ReAct agent with a *flat* tool list handles cross-domain requests natively — "add a meeting **and** a prep task" is just two tool calls in one loop. A router that first picks "the Todoist node" or "the Calendar node" has to fragment that request and re-merge it, and duplicates the agent loop per domain. You'd only reach for multi-agent/multi-node decomposition at a *scale* threshold (many domains or huge tool counts straining context) — not at integration #2 or #3.

**What actually scales with domains is context, not structure** — the flat tool list (token cost + tool-selection accuracy) and the system prompt. Both are addressed by making the registry and prompt **functions of the connected-services set** (see "Per-User Dynamic Tools & Prompts"), plus the keyword selector narrowing the per-turn toolset. This is why that refactor (Phase A) lands **before** the Calendar domain: refactor-then-extend avoids baking a two-domain assumption into three files that domain #3 would rip out.

**The one domain coupling in the otherwise domain-neutral core** is `dispatcher.py`'s `except TodoistApiError`. Phase 0 removes it by introducing a shared `ClassifiedApiError` base so the dispatcher catches the base, not per-domain types — after which no new domain touches the dispatcher at all.

### Phasing at a glance

| Phase | Scope | Behavior change? |
|-------|-------|------------------|
| **0** | Shared `ClassifiedApiError` base; dispatcher catches the base | None — pure decoupling |
| **A** | Registry + prompt + selector become functions of connected-services | None at runtime (still Todoist-only); green tests are the oracle |
| **B** | Google Calendar domain (`tools/calendar/`, OAuth lifecycle) | New capability, gated on `google_calendar` connected |
| **C** | Beta provisioning scripts | Operational only |
| **D** | Self-service `/connect` onboarding (FUTURE) | Deferred |

Ship **0 + A as one refactor PR** (no runtime change, proven by the existing suite), then **B** as a feature PR on top — isolating the structural change from the new feature.

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
    auth.py              # Supabase-backed OAuth credential load + refresh + write-back
    client.py            # GoogleCalendarClient (wraps discovery service), per-user
    schemas.py           # OpenAI function schemas + MUTATING_CALENDAR_TOOLS set
    tools.py             # get_calendar_tool_specs() + build_calendar_langchain_tools(dispatch)

agents/agent_api/app/oauth/            # NEW — service-agnostic OAuth glue
    google_flow.py       # build_consent_url(telegram_user_id), exchange_code(code, state)
    state.py             # HMAC-sign / verify the `state` param (binds link to telegram_user_id)

scripts/
    connect_todoist.py             # beta: store a user's Todoist PAT into Supabase (once)
    connect_google_calendar.py     # beta: run consent for one user, store token JSON in Supabase

# FUTURE (self-service onboarding, not built for beta):
agents/agent_api/app/api/routes/oauth_callback.py   # GET /oauth/google/callback -> exchange + store
src/services/telegram/handlers/connect-handlers.ts  # /connect command -> connector buttons + links
```

### Files to modify

| File | Change |
|------|--------|
| `agents/agent_api/app/credentials.py` | Add `get_connected_services(telegram_user_id) -> set[str]`; add `get_google_calendar_credentials` read + `save_google_calendar_credentials` write-back helpers |
| `agents/agent_api/app/tools/registry_factory.py` | Replace `build_default_registry(todoist_client)` with `build_registry_for_user(telegram_user_id, connected_services, tracer)` — register only connected domains (see "Per-User Dynamic Tools") |
| `agents/agent_api/app/graph/builder.py` | Resolve `connected_services` once, build per-user clients + registry from it, thread `first_name` + `connected_services` into prompt |
| `agents/agent_api/app/graph/prompts/orchestrator.py` | Make `get_orchestrator_prompt(tz, first_name, connected_services)` dynamic: user name line, generated "Available tools" line, Calendar tips block only when calendar connected |
| `agents/agent_api/app/graph/prompts/context.py` | Thread `first_name` + `connected_services` through `build_initial_messages` |
| `agents/agent_api/app/tools/selectors/keyword.py` | Add calendar keyword routes (harmless when calendar unregistered — see selector note) |
| `agents/agent_api/app/tools/errors.py` (**NEW, Phase 0**) | Shared `ClassifiedApiError` base (`.kind`, `.message`, `.to_classifier_payload()`); `TodoistApiError` subclasses it |
| `agents/agent_api/app/tools/dispatcher.py` | **Phase 0:** change `except TodoistApiError` → `except ClassifiedApiError` (base class). After this, no new domain edits the dispatcher; `GoogleCalendarApiError` just subclasses the base |
| `requirements.txt` | Add google-api-python-client, google-auth, google-auth-oauthlib, google-auth-httplib2 |
| `.env.sample` | Document `GOOGLE_OAUTH_CLIENT_SECRETS_JSON`, `GOOGLE_OAUTH_REDIRECT_URI`, `GOOGLE_OAUTH_STATE_SECRET`, `GOOGLE_CALENDAR_SCOPES` |
| `src/services/telegram/telegram-bot.service.ts` (FUTURE) | Register `/connect` in `syncCommands()` + wire `ConnectHandlers` |

---

## Auth, Credentials & Onboarding (Supabase)

All per-user secrets live in the existing Supabase `user_credentials` table. Nothing about the storage layer is Calendar-specific — a credential is `(user_id, service, credential_data JSONB, is_active)`. This section defines the `credential_data` shapes, how they get *in* (beta vs future onboarding), and how they get *out* (runtime resolution + refresh).

### Storage model

No schema migration needed — the generic `credential_data` JSONB already holds whatever each service requires:

| `service` | `credential_data` shape | Type |
|-----------|-------------------------|------|
| `todoist` | `{"api_key": "<PAT>"}` | Static PAT (already implemented) |
| `google_calendar` | serialized Google `Credentials` → `{"token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "expiry"}` | OAuth 2.0 |

Notes:
- The Calendar blob is exactly what `google.oauth2.credentials.Credentials.to_json()` emits and what `Credentials.from_authorized_user_info(data, SCOPES)` consumes — so serialization is a one-liner both directions.
- `client_id` / `client_secret` are **app-level**, identical for every user, and come from the Google Cloud OAuth client. They are duplicated into each blob only because the Google lib expects them there; the source of truth is the env var `GOOGLE_OAUTH_CLIENT_SECRETS_JSON`. Do **not** treat per-user rows as the client-secret store.
- The **refresh token is the crown jewel** (long-lived, full calendar access). See "Security hardening" below.

### How credentials get IN

**Beta (build this now) — Jerry provisions each user once:**

- **Todoist (PAT):** the user opens Todoist → Settings → Integrations → copies their API token → sends it to Jerry. Jerry runs `scripts/connect_todoist.py --telegram-user-id <id> --token <pat>`, which upserts `user_credentials(service='todoist', credential_data={'api_key': ...}, is_active=true)`. (This replaces the temporary `TODOIST_API_KEYS_BY_TELEGRAM_USER_ID` env map for real users; the env map stays as a fallback.)
- **Google Calendar (OAuth):** Jerry runs `scripts/connect_google_calendar.py --telegram-user-id <id>`. The script:
  1. Loads the app OAuth client from `GOOGLE_OAUTH_CLIENT_SECRETS_JSON`.
  2. Runs `InstalledAppFlow(...).run_local_server(port=0)` (or prints the consent URL for the user to open, then accepts the redirect). The user signs in **with their own Google account** and grants the calendar scope.
  3. Takes the resulting `Credentials`, calls `save_google_calendar_credentials(telegram_user_id, creds)`, which upserts the serialized blob into `user_credentials`.

  Because consent happens in a real browser against the user's own Google login, Jerry never sees the user's Google password — only the returned token. That satisfies "get the link for him, store his token once."

**Future (self-service onboarding, do NOT build for beta):**

1. User sends `/connect`. The bot replies with an inline keyboard: **[ Connect Todoist ] [ Connect Google Calendar ]**.
2. **Todoist** → bot replies with the Todoist API-token page link and asks the user to paste the token back into the chat; a message handler validates it (one live `GET /user`-style call) and stores it. (Todoist also offers OAuth; PAT-paste is simpler and fine for now.)
3. **Google Calendar** → bot builds a consent URL via `oauth/google_flow.py::build_consent_url(telegram_user_id)` and sends it as a link button. The user authorizes in-browser; Google redirects to `GET /oauth/google/callback` (new FastAPI route), which exchanges the code, stores the token, and shows a "✅ Calendar connected — return to Telegram" page. The bot confirms on the next message (or via a lightweight notify).

   The `state` param is an **HMAC-signed** encoding of `telegram_user_id` (+ nonce + expiry), verified in the callback (`oauth/state.py`). This is the security-critical piece: without it, anyone hitting the callback could bind their Google account to another user's Telegram id. Sign with `GOOGLE_OAUTH_STATE_SECRET`.

The beta scripts and the future flow **converge on the same `save_*` helpers and the same `credential_data` shape**, so moving from beta to self-service adds the Telegram command + callback route without touching storage or runtime.

### How credentials get OUT (runtime resolution + refresh)

`tools/calendar/auth.py`:

```python
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]  # or .events (narrower)

def get_calendar_credentials(telegram_user_id: int) -> Credentials:
    # 1. Load credential_data JSONB from user_credentials (service='google_calendar', is_active).
    #    Raise GoogleCalendarApiError(kind="auth", reconnect=True) if absent -> agent tells user to /connect.
    # 2. creds = Credentials.from_authorized_user_info(data, GOOGLE_CALENDAR_SCOPES)
    # 3. If creds.expired and creds.refresh_token:
    #        creds.refresh(Request())
    #        save_google_calendar_credentials(telegram_user_id, creds)   # WRITE BACK refreshed access token
    # 4. If refresh fails (revoked/expired refresh token):
    #        mark row is_active=false; raise GoogleCalendarApiError(kind="auth", reconnect=True)
    return creds
```

Key differences from the static-token design this replaces:
- **Refresh write-back is mandatory.** Access tokens expire hourly; if we don't persist the refreshed token, every request pays a refresh round-trip and (worse) a rotated refresh token would be lost. `GoogleCalendarClient` builds its service lazily per run from `get_calendar_credentials(telegram_user_id)`, mirroring `TodoistApiClient`'s per-user construction.
- **Reconnect signalling.** An `auth`/`reconnect` error is surfaced to the LLM as "your Google Calendar isn't connected (or access was revoked) — reconnect with /connect", not a generic failure. The client should not retry auth errors.

### Security hardening (note for beta, enforce before public)

- **RLS default-deny** on `user_credentials`, `user_preferences`, `users`. The agent connects with the service role (bypasses RLS via the pooled DSN); keep the anon/public key away from this table entirely.
- **Encrypt tokens at rest.** For beta, plaintext JSONB behind RLS is acceptable; before onboarding real users, wrap `credential_data` with Supabase Vault / pgcrypto (or app-layer envelope encryption) so a DB dump doesn't leak refresh tokens.
- **Least-privilege scope.** Prefer `calendar.events` over full `calendar` if the 7-tool set never touches calendar ACLs/settings. Narrowing scope later forces re-consent, so decide now.
- **Never log tokens.** The redaction rules in `CLAUDE.md` apply — no tokens/`Authorization` headers at info level. Log `service`, `telegram_user_id`, and error `kind` only.
- **HMAC the OAuth `state`** (future callback) as above; reject unsigned/expired state.

### Env vars to add

```
# App-level Google OAuth client (one client for all users). JSON from Google Cloud Console.
GOOGLE_OAUTH_CLIENT_SECRETS_JSON=   # base64 or raw JSON of the "web"/"installed" client
GOOGLE_OAUTH_REDIRECT_URI=          # future onboarding: https://<host>/oauth/google/callback
GOOGLE_OAUTH_STATE_SECRET=          # future onboarding: HMAC key for signing the state param
GOOGLE_CALENDAR_SCOPES=https://www.googleapis.com/auth/calendar   # override to narrow
# Per-user tokens are NOT env vars — they live in Supabase user_credentials.
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

> These calendar tips are a **per-domain guidance block appended only when `google_calendar` is connected** (see "Per-User Dynamic Tools & Prompts"). Todoist-only users never receive them.

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

## Per-User Dynamic Tools & Prompts

Goal: the registry the agent sees, and the system prompt it reads, are both **derived from the user's connected-services set** — not hard-coded. A user with only Todoist connected must never see Calendar tools (saves tokens, prevents the model from calling a tool that will only fail with an auth error), and must not be told in the prompt that Calendar is available. A user with both sees both. Once a third domain (Gmail, Notion) lands, it slots into the same mechanism.

### 1. Source of truth: connected services

Add to `credentials.py`:

```python
def get_connected_services(telegram_user_id: Optional[int]) -> set[str]:
    """Services with an active credential for this user. Empty set on no DB / no rows."""
    # SELECT uc.service FROM user_credentials uc
    #   JOIN users u ON u.id = uc.user_id
    #   WHERE u.telegram_user_id = %s AND uc.is_active = TRUE
    # -> {"todoist", "google_calendar"}
```

One query, resolved once per run. Env-var fallbacks still count as "connected": if `get_connected_services` is empty but `TODOIST_API_KEY` / the env token map is set, treat `todoist` as connected so single-user/dev mode keeps working. (Belt-and-suspenders: also treat a service as connected if its env fallback exists.)

### 2. Dynamic registry

Replace the static factory with a user-aware builder:

```python
# registry_factory.py
def build_registry_for_user(
    telegram_user_id: Optional[int],
    connected_services: set[str],
    tracer=None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(get_control_tool_specs())                 # always: ask_user etc.
    if "todoist" in connected_services:
        client = TodoistApiClient(tracer=tracer, telegram_user_id=telegram_user_id)
        registry.register(get_todoist_tool_specs(client), langchain_builder=build_todoist_langchain_tools)
    if "google_calendar" in connected_services:
        client = GoogleCalendarClient(tracer=tracer, telegram_user_id=telegram_user_id)
        registry.register(get_calendar_tool_specs(client), langchain_builder=build_calendar_langchain_tools)
    return registry
```

`builder.py::run_jarvis` changes: resolve `connected_services = get_connected_services(telegram_user_id)` once (with env fallback), build the registry from it, and drop the hard-coded `todoist_client` construction. Keep a back-compat path: if a caller passes an explicit `todoist_client` (the CLI runner does), honor it — treat that as a forced `todoist` registration so existing tests/CLI don't regress.

Because each client is constructed with `telegram_user_id`, the existing DB-first credential resolution already makes every tool call use *that* user's token. No tool code changes.

### 3. Dynamic prompt

`get_orchestrator_prompt(tz, first_name=None, connected_services=None)` builds the Runtime context block from the same set:

```
## Runtime context
User: {first_name or "the user"}
Current date: 2026-07-02
User timezone: Asia/Taipei
Connected services: Todoist, Google Calendar        # generated from connected_services
```

- **"Available tools" line becomes generated**, replacing the static `"Available tools: Todoist task tools only."` (this line is exactly what `context.py` already flags as needing to go dynamic "when more domains go live").
- **The Calendar tips block (see "System Prompt Changes") is appended only when `google_calendar` is connected.** Todoist-only users never carry those ~10 lines of calendar guidance. Structure the prompt as a static policy core + conditionally-concatenated per-domain guidance blocks.
- **User's first name** comes from the `users` row / `telegram_first_name` already threaded through `run_jarvis`; pass it into `build_initial_messages` → `get_system_prompt`. Enables "Good morning, Jerry"-style grounding and correct possessives.

`build_initial_messages(user_prompt, timezone, first_name=None, connected_services=None)` threads both new args through; `builder.py` passes them from the values it already has (`telegram_first_name`, resolved `connected_services`, `user_timezone`).

### 4. Selector interplay (verify, don't assume)

The tool selector (`selectors/keyword.py`, `static.py`) chooses which registered tools the model sees per turn. Since the registry is now per-user, the selector only ever sees connected tools — **provided it intersects its requested tool names with what's actually in the registry.** Confirm `static`/`keyword` selectors filter to `registry` membership; if a keyword route names `create_calendar_event` but calendar isn't registered, that name must be silently dropped, not surfaced. Add a unit test for "keyword route referencing an unregistered domain is a no-op."

### 5. Degenerate case: zero services connected

A brand-new user with no credentials gets only control tools. The prompt should say no task/calendar services are connected and instruct the agent to tell the user to connect one (beta: "ask Jerry to connect you"; future: "/connect"). Don't let the agent attempt task/calendar work with an empty toolset.

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

**Phase 0 — decouple the dispatcher (tiny, independent, no behavior change):**
0. `tools/errors.py` — add `ClassifiedApiError` base; make `TodoistApiError` subclass it; change `dispatcher.py`'s `except TodoistApiError` → `except ClassifiedApiError`. Existing Todoist tests must stay green.

**Phase A — per-user plumbing (service-agnostic, unblocks everything else):**
1. `credentials.py` — `get_connected_services()` + calendar cred read/write helpers
2. `tools/registry_factory.py` — `build_registry_for_user(...)` (dynamic registration + env fallback + back-compat)
3. `graph/prompts/orchestrator.py` + `prompts/context.py` — dynamic prompt (name, generated tools line, conditional blocks)
4. `graph/builder.py` — resolve `connected_services` once; build registry + prompt from it
5. Tests: Todoist-only user still works end-to-end; zero-service user gets only control tools; selector drops unregistered names

**Phase B — Google Calendar domain:**
6. `requirements.txt` — add google-api-python-client, google-auth, google-auth-oauthlib, google-auth-httplib2
7. `tools/calendar/auth.py` — Supabase-backed credential load + refresh + write-back
8. `tools/calendar/client.py` — per-user API client, all 7 methods
9. `tools/calendar/schemas.py` — schemas + MUTATING_CALENDAR_TOOLS
10. `tools/calendar/tools.py` + `__init__.py` — specs + LangChain builders + re-exports
11. `tools/registry_factory.py` — add the `google_calendar` branch
12. `tools/calendar/client.py` — ensure `GoogleCalendarApiError` subclasses `ClassifiedApiError` (with `reconnect` signalling). No dispatcher edit needed — Phase 0 already catches the base
13. `graph/prompts/orchestrator.py` — append Calendar tips block when connected
14. `tools/selectors/keyword.py` — calendar keyword routes
15. `.env.sample` — document Google OAuth env vars

**Phase C — beta provisioning:**
16. `oauth/state.py` + `oauth/google_flow.py` — consent URL + code exchange (state.py used by future callback)
17. `scripts/connect_todoist.py` — store a user's PAT
18. `scripts/connect_google_calendar.py` — run consent for one user, store token
19. End-to-end: connect a real beta user's calendar, verify a live `list_calendars`

**Phase D — self-service onboarding (FUTURE, not for beta):**
20. `api/routes/oauth_callback.py` — `GET /oauth/google/callback`
21. `src/services/telegram/handlers/connect-handlers.ts` + `/connect` in `syncCommands()`

---

## Verification

**Per-user tools & prompts (Phase A):**
1. **Dynamic registry**: mock `get_connected_services` → `{"todoist"}` yields no calendar specs; `{"todoist","google_calendar"}` yields both; `set()` yields control-only.
2. **Env fallback**: no DB rows but `TODOIST_API_KEY` set → `todoist` still registered.
3. **Dynamic prompt**: first name appears; "Available tools"/"Connected services" line matches the set; Calendar tips block present iff calendar connected.
4. **Selector safety**: a keyword route naming an unregistered calendar tool is dropped, not surfaced.
5. **No regression**: Todoist-only user completes an existing task flow end-to-end unchanged.

**Calendar domain (Phase B):**
6. **Unit tests**: schema registration, layer consistency (schema names == spec names == langchain tool names), client method routing with a mocked Google service.
7. **Mutation guard**: `delete_calendar_event` blocked when `ALLOW_MUTATIONS=false`.
8. **Conflict detection**: creating at a busy time makes the LLM call `get_freebusy` first and warn.

**Auth & credentials (Phase B/C):**
9. **Refresh write-back**: expired access token + valid refresh token → `creds.refresh()` runs *and* the new token is persisted to Supabase (assert a write happened; second run does not re-refresh).
10. **Revoked reconnect**: refresh failure → row flipped `is_active=false` and a `kind="auth", reconnect=True` error surfaces as a "reconnect your calendar" message, not a crash; client does not retry.
11. **Per-user isolation**: two users' calls resolve two different tokens; no env/global leakage.
12. **No secret logging**: grep run logs — no tokens or `Authorization` headers at info level.

**End-to-end (Phase C):**
13. Provision a real beta user via `scripts/connect_google_calendar.py`; over Telegram send "what's on my calendar today?" and "schedule a meeting tomorrow at 2pm called 'standup'" — verify LLM → tool → Calendar API → reply.

**Onboarding (Phase D, when built):**
14. **State tamper**: `/oauth/google/callback` with an unsigned or expired `state` is rejected; a valid signed state binds the token to the correct `telegram_user_id`.

---

## Key References

- Google Calendar API Python quickstart: https://developers.google.com/workspace/calendar/api/quickstart/python
- MCP reference for tool ideas: https://github.com/nspady/google-calendar-mcp/blob/main/src/tools/registry.ts
- Existing Todoist tool pattern: `agents/agent_api/app/tools/todoist/` (schemas.py, client.py, tools.py)
- Registry factory: `agents/agent_api/app/tools/registry_factory.py`
- Tool selector: `agents/agent_api/app/tools/selectors/keyword.py`
- System prompt: `agents/agent_api/app/graph/prompts/orchestrator.py`
