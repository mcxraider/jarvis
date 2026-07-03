# Plan: Todoist Per-User Token Routing — Parity with Calendar

## Context

Todoist per-user token routing **already exists** and is working:
- `TODOIST_API_KEYS_BY_TELEGRAM_USER_ID` env var (format: `telegram_id:api_key,id2:key2`)
- `_parse_todoist_token_map()` parses it
- `todoist_api_key_for_telegram_user()` resolves: DB → env map → fallback `TODOIST_API_KEY`
- `TodoistApiClient(telegram_user_id=...)` uses the resolved key
- `builder.py` passes `telegram_user_id` to the client

However, now that Calendar has a robust file-based multi-user token system (`tokens/` directory with per-user files), we should bring Todoist to **full parity** with the same developer experience:

1. **Consistent token storage** — Todoist keys currently live only in the `.env` file (as env var values). For consistency with the Calendar pattern, optionally support file-based Todoist API keys in `tokens/` as well (useful if Jerry wants to keep secrets out of `.env`).
2. **Robust test coverage** — Calendar has 19 tests for parsing/resolution. Todoist only has 3+4=7. Add edge case coverage matching Calendar's pattern.
3. **Documentation parity** — Ensure `.env.sample` has the same quality documentation for Todoist as Calendar now has.

## Stage 1: Expanded Test Coverage for Existing Todoist Token Routing

**Goal:** Mirror the comprehensive test coverage from `test_calendar_multi_user.py` for Todoist's `_parse_todoist_token_map`.

### Tests to add (in `tests/agents/test_todoist_token_mapping.py`):

- `test_parse_none_returns_empty` 
- `test_parse_empty_string_returns_empty`
- `test_parse_whitespace_only_returns_empty`
- `test_parse_single_entry`
- `test_parse_multiple_entries`
- `test_parse_strips_whitespace`
- `test_parse_trailing_comma_ignored`
- `test_parse_malformed_no_colon_raises`
- `test_parse_malformed_empty_id_raises`
- `test_parse_malformed_empty_token_raises`
- `test_none_telegram_user_id_returns_fallback`
- `test_user_in_map_returns_mapped_key`
- `test_user_not_in_map_returns_none_when_map_configured`

### Files modified:
- `tests/agents/test_todoist_token_mapping.py` — expand with edge case tests

---

## Stage 2: File-Based Todoist Token Support (Optional Enhancement)

**Goal:** Allow Todoist API keys to be stored as individual files in `tokens/` (e.g. `tokens/jerry_todoist.key`), mirroring how Calendar stores per-user `token.json` files. This keeps API keys out of `.env` entirely.

### Changes:

1. **`agents/agent_api/app/tools/todoist/client.py`:**
   - Add env var `TODOIST_KEY_FILES_BY_TELEGRAM_USER_ID` (format: `id:path,id:path`)
   - Add `_parse_todoist_key_file_map(raw_value)` — same parser pattern
   - Add `_read_todoist_key_file(path: str) -> Optional[str]` — reads a single-line file, strips whitespace
   - Update `todoist_api_key_for_telegram_user()` resolution chain:
     - DB → env var key map → **file-based key map** → fallback `TODOIST_API_KEY`

2. **`.env.sample`** — document the new file-based option

### Tests:
- `test_file_based_key_reads_from_path` — create a temp file with an API key, verify it's read
- `test_file_based_key_takes_priority_over_env_var_map` — file map wins over inline env var
- `test_file_based_key_missing_file_falls_through` — graceful fallback when file doesn't exist
- `test_file_based_key_strips_whitespace_and_newlines` — handles trailing newline in key files

### Files modified:
- `agents/agent_api/app/tools/todoist/client.py`
- `.env.sample`
- `tests/agents/test_todoist_token_mapping.py`

---

## Stage 3: Documentation Parity

**Goal:** Ensure `.env.sample` clearly documents both approaches (inline env var vs file-based) for both Todoist and Calendar, in a consistent format.

### Changes:
- Update `.env.sample` Todoist section with file-based option documentation
- Ensure the friend onboarding instructions in `plans/multi-user-mvp.md` mention "For Todoist: go to todoist.com/app/settings/integrations/developer → copy API token → send to Jerry"

---

## Verification

1. Run all existing Todoist tests → still pass
2. Run all new Todoist token routing tests → pass
3. Run the full `tests/agents/` suite → no regressions
4. Manual: set up 2 users with different Todoist keys, verify each gets their own tasks
