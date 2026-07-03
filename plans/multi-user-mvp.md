# Plan: Multi-User Support (3-User MVP, Local Laptop)

## Context

Jarvis currently hardcodes "Jerry" in the orchestrator prompt and uses a single `token.json` for Google Calendar. Todoist already supports per-user routing via `TODOIST_API_KEYS_BY_TELEGRAM_USER_ID`. The goal: up to 3 friends use the bot running entirely on Jerry's laptop, each with their own Todoist + Calendar credentials.

**Key constraint:** Friends are not developers and don't host anything. Everything runs on Jerry's laptop. Friends only need to generate a Google Calendar token once (via a simple script Jerry sends them) and hand it back.

---

## Stage 1: Per-User Google Calendar Token Routing

**Goal:** The system reads per-user token files from a `tokens/` directory, routed by Telegram user ID — same pattern as Todoist.

### Changes

1. **Create `tokens/` directory + `.gitkeep`** at repo root
2. **`.gitignore`** — add `tokens/` (but not `.gitkeep`)
3. **`agents/agent_api/app/tools/calendar/auth.py`:**
   - Add env var constant `GOOGLE_TOKEN_MAP_ENV = "GOOGLE_TOKEN_PATHS_BY_TELEGRAM_USER_ID"`
   - Add `_parse_google_token_map(raw_value: Optional[str]) -> Dict[str, str]` — parse `"id:path,id:path"` format
   - Add `get_token_path_for_user(telegram_user_id: Optional[int] = None) -> str` — resolution: env map → `get_token_path()` fallback
   - Add `is_calendar_configured_for_user(telegram_user_id: Optional[int] = None) -> bool`
   - Modify `load_credentials(token_path: Optional[str] = None)` — accept explicit path, default to `get_token_path()`
   - Modify `build_calendar_service(token_path: Optional[str] = None)` — pass through
4. **`agents/agent_api/app/tools/calendar/client.py`:**
   - Constructor gains `token_path: Optional[str] = None`, stores as `self._token_path`
   - `service` property passes `self._token_path` to `build_calendar_service()`
5. **`agents/agent_api/app/graph/builder.py`** (lines 386-389):
   - Replace `is_calendar_configured()` with `is_calendar_configured_for_user(telegram_user_id)`
   - Pass `token_path=get_token_path_for_user(telegram_user_id)` to `GoogleCalendarClient`
6. **`.env.sample`** — document the new env var

### Stage 1 Tests

- **`test_parse_google_token_map`** — valid parsing, empty string, malformed entries raise ValueError
- **`test_get_token_path_for_user`** — env map hit returns mapped path; no map returns default; None user returns default
- **`test_is_calendar_configured_for_user`** — returns True when user's token file exists, False when it doesn't, falls back correctly
- **`test_load_credentials_with_explicit_path`** — mock token file at a custom path, verify it's loaded from there (not the default)
- **`test_builder_routes_calendar_per_user`** — mock two token files; call `run_jarvis` with user A → calendar enabled; call with user B (no token) → calendar is None, no crash
- **Manual verification:** Move your existing `token.json` to `tokens/jerry_token.json`, set `GOOGLE_TOKEN_PATHS_BY_TELEGRAM_USER_ID=701122767:tokens/jerry_token.json` in `.env`, restart bot, send a calendar command from Telegram → works as before

---

## Stage 2: Dynamic Orchestrator Prompt (User Name)

**Goal:** Replace hardcoded "Jerry" with the requesting user's actual Telegram first name. Also make "Available tools" line reflect whether this user has calendar configured.

### Changes

1. **`agents/agent_api/app/graph/prompts/orchestrator.py`:**
   - Add `_build_role_line(user_name: str = "the user") -> str` — interpolates name into the role sentence
   - Keep static `ORCHESTRATOR_PROMPT` constant (for backward compat in tests)
   - Modify `get_orchestrator_prompt(tz, user_name=None, calendar_enabled=True)` — use `_build_role_line(user_name or "the user")`, conditionally set "Available tools" line
   - Modify `get_system_prompt(timezone, user_name=None, calendar_enabled=True)` — pass through

2. **`agents/agent_api/app/graph/prompts/context.py`:**
   - `build_initial_messages(user_prompt, timezone, user_name=None, calendar_enabled=True)` — pass new params to `get_system_prompt`

3. **`agents/agent_api/app/graph/builder.py`:**
   - `build_initial_state(...)` gains `user_name: Optional[str] = None`, `calendar_enabled: bool = True`
   - In `run_jarvis()` at the `app.invoke(build_initial_state(...))` call (line ~450): pass `user_name=telegram_first_name` and `calendar_enabled=calendar_enabled`

### Stage 2 Tests

- **`test_build_role_line_dynamic`** — `_build_role_line("Zachary")` → contains "Zachary's personal assistant"
- **`test_get_orchestrator_prompt_with_name`** — full prompt contains user name, not "Jerry"
- **`test_available_tools_line_conditional`** — `calendar_enabled=False` → "Available tools: Todoist task tools." (no calendar mention)
- **`test_build_initial_messages_threads_name`** — verify system message content includes the provided user_name
- **`test_existing_tests_still_pass`** — run the full test suite to confirm backward compat (default params = old behavior)
- **Manual verification:** Send message from Telegram → check logs or LangSmith trace that the system prompt says "[Your Name]'s personal assistant" instead of "Jerry's"

---

## Stage 3: Friend Onboarding Script + Instructions

**Goal:** Create a standalone mini script that a friend can run to generate their `token.json`, plus clear instructions Jerry can send them.

### Changes

1. **Create `scripts/generate_friend_token.py`** — a minimal standalone script (no project imports) that:
   - Takes `credentials.json` as input (same directory or arg)
   - Runs the OAuth browser consent flow
   - Writes `my_token.json` in the current directory
   - Prints "Done! Send my_token.json back to Jerry."
   - Includes inline pip install instructions in comments at top

2. **Update `.env.sample`** — final consolidated documentation for all multi-user env vars with example values

### Script contents (standalone, no repo dependency):
```python
"""Run this to authorize your Google Calendar for Jarvis.
pip install google-auth google-auth-oauthlib google-api-python-client
Then: python generate_friend_token.py
"""
# reads credentials.json from same folder, opens browser, writes my_token.json
```

### Stage 3 Tests

- **`test_generate_friend_token_script_syntax`** — import the module (no execution) to verify no syntax errors
- **`test_script_fails_gracefully_without_credentials`** — run with no credentials.json, verify clear error message
- **Manual verification:** Run the script yourself with your own Google account (use a different `--output` path), verify it produces a valid token file that works when placed in `tokens/`

---

## Stage 4: End-to-End Multi-User Verification

**Goal:** Full integration test with 2+ users configured.

### Tests

- **Full flow test (Jerry):** Telegram message → calendar + todoist both work, prompt personalized
- **Full flow test (Friend with both services):** Telegram message → uses friend's Todoist key + friend's Calendar token, prompt says friend's name
- **Full flow test (Friend with Todoist only, no calendar):** Telegram message → Todoist works, no calendar tools exposed, no crash, prompt says "Todoist task tools" only
- **Graceful degradation:** User not in `ALLOWED_TELEGRAM_USER_IDS` → rejected as before (no regression)
- **Token refresh test:** Expire a token, verify auto-refresh writes back to the per-user path (not the default `token.json`)

---

## Summary of Files Modified

| File | Stage |
|------|-------|
| `.gitignore` | 1 |
| `tokens/.gitkeep` (new) | 1 |
| `agents/agent_api/app/tools/calendar/auth.py` | 1 |
| `agents/agent_api/app/tools/calendar/client.py` | 1 |
| `agents/agent_api/app/graph/builder.py` | 1, 2 |
| `.env.sample` | 1, 3 |
| `agents/agent_api/app/graph/prompts/orchestrator.py` | 2 |
| `agents/agent_api/app/graph/prompts/context.py` | 2 |
| `scripts/generate_friend_token.py` (new) | 3 |

---

## Friend Onboarding Flow (what Jerry sends them)

> **Hey! Here's how to connect your Google Calendar to Jarvis:**
> 1. I'm sending you two files: `credentials.json` and `generate_friend_token.py`
> 2. Put them in the same folder
> 3. Install Python if you don't have it (python.org)
> 4. Open terminal/command prompt in that folder and run:
>    ```
>    pip install google-auth google-auth-oauthlib google-api-python-client
>    python generate_friend_token.py
>    ```
> 5. Your browser opens → log into your Google account → click "Allow"
> 6. You'll see "Done!" and a file called `my_token.json` appears
> 7. Send me `my_token.json` (Signal/AirDrop/whatever)
> 8. That's it! I'll set it up on my end.
>
> **For Todoist:** Go to todoist.com/app/settings/integrations/developer → copy your API token → send it to me.
