# Google Calendar Integration (Single-User) — Staged Development Plan

## Context

Jarvis is Jerry's single-user Telegram assistant. The Python LangGraph agent
(`agents/agent_api/app/`) currently exposes one tool domain — Todoist. This plan adds
**Google Calendar** as a second domain (list/create/update/delete events, list calendars,
free/busy) through the same conversational loop.

We reference `plans/gcal-integration.md`, but that doc targets a **multi-user
Supabase-OAuth** design that does not match the current code:
- `registry_factory.py` is still static `build_default_registry(todoist_client)`.
- `credentials.py` resolves only a single `api_key` string — not OAuth blobs.
- No `get_connected_services`, no OAuth `state`/callback, no `/connect`.

Per the constraint **"only me, no multi-user"**, we **drop the doc's Phases A / C / D**
(dynamic per-user registry, HMAC state, self-service onboarding) and mirror how Todoist is
wired today. Auth = **local `token.json`** (gitignored) + full `calendar` scope.

### The pattern we mirror
Todoist is a 3-file domain under `tools/todoist/`: `client.py` (REST + `TodoistApiError`),
`schemas.py` (LLM schemas + `MUTATING_TOOL_NAMES`), `tools.py`
(`get_todoist_tool_specs(client)` → `list[ToolSpec]`, `build_todoist_langchain_tools(dispatch)`),
registered in one line in `registry_factory.py`. The graph core is domain-neutral.

### The one non-additive coupling (must handle in Stage 5)
Confirmation gating is NOT registry-driven. `graph/risk.py:13` imports `MUTATING_TOOL_NAMES`
from `todoist.schemas`, and `tools/metadata.py::_REGISTRY` hardcodes Todoist display/risk
metadata (read via `get_meta()` by prepare_confirm/confirm/executor). For
`delete_calendar_event` to be gated like `delete_todoist_task`, those two files need calendar
entries.

## Design Decisions
| Decision | Choice |
|----------|--------|
| Token storage | Local `token.json` (gitignored) |
| OAuth scope | `https://www.googleapis.com/auth/calendar` (full) |
| Client secret | Env `GOOGLE_OAUTH_CLIENT_SECRETS_JSON` (raw JSON) |
| Registry | Extend static `build_default_registry(...)`; no dynamic per-user builder |
| Calendar enablement | Register calendar only when a local `token.json` exists (the token embeds client id/secret, so it is necessary and sufficient at runtime — the client-secret env is only used by the Stage 7 connect script) |
| `event_id` grounding | Prompt-level only (fail-open, like `add_comment`); structural grounding deferred |

## MVP Tool Set (7 tools)
`list_calendars`, `list_calendar_events`, `get_calendar_event`, `get_freebusy` (reads);
`create_calendar_event`, `update_calendar_event` (mutating, bulk-gated);
`delete_calendar_event` (mutating, **always-risky + irreversible**).

---

# Development Stages

**Ground rule:** each stage ends with a **Test Gate**. Do not start the next stage until
its gate is green. New Python tests go under `tests/agents/` as `test_calendar_*.py`,
mirroring existing pytest patterns. Run the Python suite with the agent venv:
`python -m pytest tests/agents -q` (or the repo's documented `tests/README.md` runner).

---

## Stage 0 — Foundation: dependencies + error decouple

**Goal:** install Google libs and make the dispatcher domain-agnostic for classified errors,
with zero behavior change.

**Do:**
1. `requirements.txt` += 
   ```
   google-api-python-client==2.149.0
   google-auth==2.35.0
   google-auth-httplib2==0.2.0
   google-auth-oauthlib==1.2.1
   ```
   then `pip install -r requirements.txt` in the agent venv.
2. `.gitignore` += `token.json` and `google_client_secret*.json`.
3. New `tools/errors.py`: `class ClassifiedApiError(Exception)` with `.kind`, `.message`,
   `.retryable`, `.to_classifier_payload()` (lift the shape currently on `TodoistApiError`).
4. `todoist/client.py`: make `TodoistApiError(ClassifiedApiError)` (keep every field/behavior).
5. `dispatcher.py`: change import + `except TodoistApiError` (line 34 / 271) →
   `except ClassifiedApiError`.

**Test Gate 0:**
- `python -c "import googleapiclient, google.oauth2.credentials, google_auth_oauthlib"` succeeds.
- New `test_calendar_errors.py`: `issubclass(TodoistApiError, ClassifiedApiError)`;
  `TodoistApiError(kind="auth", message="x").to_classifier_payload()["kind"] == "auth"`.
- **Full existing Python suite still passes** (this is the regression oracle proving the
  dispatcher change is behavior-neutral): `python -m pytest tests/agents -q`.

---

## Stage 1 — Auth layer (`tools/calendar/auth.py`)

**Goal:** load/refresh/persist the local OAuth token; expose a configured-check + service builder.

**Do:**
```python
GOOGLE_CALENDAR_SCOPES = os.getenv("GOOGLE_CALENDAR_SCOPES",
    "https://www.googleapis.com/auth/calendar").split()
TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "token.json")

def load_credentials() -> Credentials:
    # from_authorized_user_file(TOKEN_PATH, SCOPES); missing -> GoogleCalendarApiError(
    #   kind="auth", reconnect=True, "Calendar not connected; run connect script").
    # if expired and refresh_token: refresh(Request()); write creds.to_json() to TOKEN_PATH.
    # refresh failure -> GoogleCalendarApiError(kind="auth", reconnect=True).
def build_calendar_service():  # discovery.build("calendar","v3",credentials=...,cache_discovery=False)
def is_calendar_configured() -> bool:  # TOKEN_PATH exists (token embeds client id/secret; sufficient at runtime)
```
`GoogleCalendarApiError(ClassifiedApiError)` may live here or in `client.py` (Stage 2) —
define it here so auth can raise it. Never log token contents.

**Test Gate 1** (`test_calendar_auth.py`, all mocked — no network/browser):
- Missing token file → `load_credentials()` raises `GoogleCalendarApiError(kind="auth",
  reconnect=True)`.
- Valid non-expired token (fixture JSON in `tmp_path`, monkeypatch `GOOGLE_TOKEN_PATH`) →
  returns creds, **no** refresh call, file unchanged.
- Expired token + refresh_token: monkeypatch `Credentials.refresh` to flip
  `expired→False` → `load_credentials()` calls refresh **and rewrites `token.json`**;
  a second `load_credentials()` does not refresh again.
- `is_calendar_configured()` = False when the token file is absent; True when it exists
  (the client-secret env is not required at runtime).

---

## Stage 2 — Client (`tools/calendar/client.py`)

**Goal:** all 7 methods against the discovery service, normalized outputs, classified errors,
retry — mirroring `TodoistApiClient`.

**Do:**
```python
class GoogleCalendarClient:
    def __init__(self, tracer=None): self._service = None  # lazy build_calendar_service()
    # methods take arguments: Dict, return cleaned Dict
    def list_calendars / list_calendar_events / get_calendar_event / get_freebusy
    def create_calendar_event / update_calendar_event / delete_calendar_event
```
- Shared `_execute(request, operation)`: run `.execute()`, classify `HttpError` by status
  (401/403→auth, 404→not-found, 429→rate-limit, 5xx→transient, 4xx→validation), retry
  transient/rate-limit, trace `calendar.request/response/error` (no payloads with tokens).
- Normalize events to `{event_id, summary, start, end, location, attendees, status}`;
  mutations return `{"success": True, ...}`.
- `calendar_id` defaults to `"primary"`.

**Test Gate 2** (`test_calendar_client.py`, **mocked Google service** — inject a fake
`service` so no network):
- Each of the 7 methods calls the correct resource chain (e.g. `events().list(...).execute()`,
  `events().delete(...).execute()`, `freebusy().query(...).execute()`) with expected params
  (`calendar_id` defaults to `"primary"`; RFC 3339 passthrough).
- `list_calendar_events` returns the **normalized** shape (asserts verbose Google fields
  are stripped).
- A fake `HttpError(status=404)` → `GoogleCalendarApiError(kind="not-found")`;
  `status=429` retried then raised as `kind="rate-limit"`.
- `delete_calendar_event` returns `{"success": True, ...}`.

---

## Stage 3 — Schemas + tools layer (`tools/calendar/schemas.py`, `tools.py`, `__init__.py`)

**Goal:** LLM contract + ToolSpec/LangChain wiring, identical shape to Todoist.

**Do:**
- `schemas.py`: 7 OpenAI/DeepSeek function schemas (explicit JSON Schema,
  `additionalProperties:false`) — reuse the drafts in `plans/gcal-integration.md`.
  `MUTATING_CALENDAR_TOOLS = {"create_calendar_event","update_calendar_event","delete_calendar_event"}`.
- `tools.py`: `get_calendar_tool_specs(client) -> list[ToolSpec]` (pair schema-by-name with
  client method, `mutating=name in MUTATING_CALENDAR_TOOLS`); `build_calendar_langchain_tools(dispatch)`
  (one `@tool` per fn → `dispatch(tool_call_id, name, args)`).
- `__init__.py`: replace placeholder; re-export `get_calendar_tool_specs`,
  `build_calendar_langchain_tools`, `GoogleCalendarClient`, `GoogleCalendarApiError`,
  `MUTATING_CALENDAR_TOOLS`.

**Test Gate 3** (`test_calendar_tools.py`):
- **Layer consistency (the key structural test):** the set of `function.name` in schemas ==
  names from `get_calendar_tool_specs(mock_client)` == `.name` of
  `build_calendar_langchain_tools(lambda *a: None)` == all 7 expected names.
- `get_calendar_tool_specs` marks exactly the 3 mutating tools `mutating=True`.
- Each spec's `handler` is the matching client method; each langchain tool, when invoked,
  calls `dispatch` with the right `(name, args)`.

---

## Stage 4 — Registry + builder wiring

**Goal:** calendar tools reach the agent when configured; no regression when not.

**Do:**
- `registry_factory.py`: `build_default_registry(todoist_client, calendar_client=None)` →
  register calendar block only when `calendar_client is not None`.
- `builder.py::run_jarvis` (near lines 379–390): after `todoist_client`, build
  `calendar_client = GoogleCalendarClient(tracer=tracer) if is_calendar_configured() else None`;
  include it in the tracer-retarget loop; pass to `build_default_registry`.
- `runner.py`: no change (calendar built inside `run_jarvis`) — confirm back-compat.

**Test Gate 4** (`test_calendar_registry.py`):
- `build_default_registry(mock_todoist, calendar_client=mock_cal)` exposes all 7 calendar
  schemas + Todoist + `ask_user`; no duplicate-name error.
- `build_default_registry(mock_todoist, calendar_client=None)` exposes **zero** calendar
  tools and the Todoist set is byte-for-byte unchanged.
- `run_jarvis` smoke: monkeypatch `is_calendar_configured→True` and inject fake clients →
  the compiled dispatcher's `registry` contains calendar tool names (no live API/LLM call;
  reuse the existing run_jarvis test harness with mocked `agent_client`).

---

## Stage 5 — Gating (`graph/risk.py`, `tools/metadata.py`)

**Goal:** `delete_calendar_event` always confirms; create/update count toward the 5+ bulk gate.

**Do:**
- `risk.py`: `from ...tools.calendar.schemas import MUTATING_CALENDAR_TOOLS`;
  `MUTATING_TOOLS = frozenset(MUTATING_TOOL_NAMES | MUTATING_CALENDAR_TOOLS)`.
- `metadata.py::_REGISTRY` add: `delete_calendar_event` (verb="deleting", label="Delete event",
  irreversible=True, always_risky=True, render_fn showing summary/id); `create_calendar_event`
  (label="Create event", highlight_arg="summary"); `update_calendar_event` (label="Update event",
  render_fn listing changed fields). Reads (`get_*`, `list_*`, `get_freebusy`) need no entry.
- prepare_confirm/confirm/executor untouched (metadata-driven).

**Test Gate 5** (extend `test_calendar_registry.py` or new `test_calendar_risk.py`):
- `classify_risk({delete_calendar_event}, state)` == "risky" at count 0.
- `create_calendar_event` == "low" alone; == "risky" once turn mutation count ≥ 5.
- With `JARVIS_ALLOW_MUTATIONS=false`, dispatcher blocks `delete_calendar_event`
  (result `mutation_blocked=True`).
- `always_risky_tools()` includes `delete_calendar_event`; `irreversible_tools()` includes it.
- **Regression:** existing risk/confirm tests still green.

---

## Stage 6 — Prompt + selector (`orchestrator.py`, `keyword.py`)

**Goal:** the model knows Calendar exists, how to use it, and gets narrowed tool sets.

**Do:**
- `orchestrator.py`: replace the "Todoist is Jerry's single app for BOTH…" line with
  Todoist=tasks / Calendar=events routing; extend Grounding to `event_id` (fetch before
  mutate); add a "## Google Calendar tool tips" block (RFC 3339 + tz; timed vs all-day, end
  exclusive; default `calendar_id="primary"`; `get_freebusy` before booking; RRULE;
  attendees=emails) per `plans/gcal-integration.md`; fix the "Available tools" runtime line.
- `keyword.py`: add calendar routes (`schedule`, `meeting`, `appointment`, `event`, `calendar`,
  `free`/`busy`/`available`, `cancel meeting`, `reschedule meeting`, `move meeting`).

**Test Gate 6** (`test_calendar_selector.py`):
- **Selector safety:** with a Todoist-only registry, a query containing "meeting" returns
  **no** calendar schemas (unregistered names dropped, not surfaced).
- With calendar registered, "when am I free tomorrow" selects `get_freebusy`;
  "schedule a standup" selects `create_calendar_event` (+ `list_calendar_events`/`get_freebusy`);
  `ask_user` always present.
- Prompt: `get_orchestrator_prompt()` contains the Calendar tips block and the two-domain
  routing line; snapshot/`assert "Google Calendar" in prompt`.

---

## Stage 7 — Connect script + live end-to-end

**Goal:** produce a real token and prove the full LLM→tool→Calendar→reply path.

**Do:**
- `scripts/connect_google_calendar.py`: load secret from `GOOGLE_OAUTH_CLIENT_SECRETS_JSON`,
  `InstalledAppFlow(...).run_local_server(port=0)`, write `creds.to_json()` to `token.json`.
- `.env.sample`: document `GOOGLE_OAUTH_CLIENT_SECRETS_JSON`, `GOOGLE_TOKEN_PATH`,
  `GOOGLE_CALENDAR_SCOPES`.

**Test Gate 7 (manual, live — `JARVIS_ALLOW_MUTATIONS=true`):**
1. Run the connect script once → `token.json` created; `is_calendar_configured()` True.
2. `uvicorn agents.api:app --host 127.0.0.1 --port 8000`; over Telegram:
   - "what's on my calendar today?" → `list_calendar_events` → correct reply.
   - "am I free tomorrow 2–3pm?" → `get_freebusy`.
   - "schedule a standup tomorrow 2pm for 30 min" → freebusy check + `create_calendar_event`
     (verify event appears in Google Calendar).
   - "cancel the standup" → fetch + `delete_calendar_event` → **confirm gate fires** →
     approve → event removed.
3. Grep `logs/app.log` + the per-run log: **no tokens / `Authorization` headers** at info level.
4. `npm run build` && `npm run lint` (TS layer untouched — sanity only).

---

## Final Regression Gate (before merge)
- Full Python suite green: `python -m pytest tests/agents -q`.
- Calendar disabled (no `token.json`): Todoist-only flow works unchanged end-to-end.
- Calendar enabled: a cross-domain prompt ("add a prep task and schedule the meeting")
  drives both a Todoist and a Calendar tool call in one turn.

## Out of Scope (deferred, per single-user constraint)
Dynamic per-user registry/prompt (`get_connected_services`); Supabase OAuth storage; OAuth
`state` HMAC + `/oauth/google/callback` + `/connect`; structural `event_id` grounding via the
entity index; deferred tools (`search_events`, `respond_to_event`, bulk `create_events`,
`list_colors`).