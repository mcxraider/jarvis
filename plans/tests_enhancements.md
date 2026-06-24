# Test Enhancements — Implementation Report

> Generated: 2026-06-24  
> Status: **Phase A + B + C (partial) implemented**

---

## Results Summary

| # | Gap | Test File | Tests | Status |
|---|-----|-----------|-------|--------|
| 1 | Error Classification | `tests/unit/services/telegram/errors/classified-error.test.ts` | 29 | **PASS** |
| 2a | Contract Sync (TS) | `tests/contract/agent-contract.test.ts` | 15 | **PASS** |
| 2b | Contract Sync (Py) | `tests/agents/test_contract.py` | 17 | **PASS** |
| 3 | Streaming NDJSON Edge Cases | `tests/unit/services/ai/langgraph-agent-client.service.test.ts` (+7) | 7 new | **PASS** |
| 4 | Tool Dispatcher | `agents/tests/test_dispatcher.py` | 13 | **PASS** |
| 5 | DeepSeek Retry | `agents/tests/test_deepseek_client.py` | 11 | **PASS** |
| 6 | route_after_agent | `agents/tests/test_edges_route_after_agent.py` | 11 | **PASS** |
| 7 | Audio Converter | `tests/unit/utils/ai/audioConverter.test.ts` | 18 | **PASS** |
| 14 | BotActivityService | `tests/unit/services/telegram/bot-activity.service.test.ts` | 18 | **PASS** |

**Total new tests: 139** (all passing)

---

## Test Run Results

### TypeScript (`npm test -- --runInBand`)
```
Test Suites: 22 passed, 22 total
Tests:       180 passed, 180 total
Time:        1.071 s
```

### TypeScript Contract Tests (`npx jest --testPathPattern 'tests/contract'`)
```
Test Suites: 1 passed, 1 total
Tests:       15 passed, 15 total
Time:        0.627 s
```

### Python (`python -m pytest agents/tests/ tests/agents/ -v`)
```
197 passed, 2 failed
```

**The 2 failures are PRE-EXISTING** in `tests/agents/test_jarvis.py` (not our new tests):
1. `test_resume_appends_hitl_tool_message_and_user_reply` — Expects old HITL message format (`"the dentist task"`) but the code now sends a richer clarification-received message.
2. `test_system_prompt_uses_orchestrator_contract` — Expects old prompt string `"You are Jarvis, the Jerry's personal orchestrator agent."` but the prompt has been rewritten.

These are stale assertions in old tests that haven't been updated to match recent prompt/message-format changes.

---

## Shared Contract Fixtures

Created at `tests/contract/fixtures/`:
- `response-completed.json` — completed response with tool_results
- `response-interrupted-confirm.json` — confirm-type interrupt
- `response-interrupted-clarify.json` — clarify-type interrupt
- `response-failed.json` — failed response with error
- `stream-progress.json` — NDJSON progress event
- `stream-final.json` — NDJSON final event wrapping AgentResponse
- `invoke-request.json` — InvokeRequest payload
- `resume-request.json` — ResumeRequest payload

Both the TS and Python contract tests validate against these same fixtures, so any schema drift between the two languages will be caught.

---

## Jest Configuration Note

The contract tests live at `tests/contract/` which is outside the default `testMatch` pattern (`**/tests/unit/**/*.test.ts`). To include them in CI, either:
1. Add `'**/tests/contract/**/*.test.ts'` to the `testMatch` array in `jest.config.js`, OR
2. Run them separately: `npx jest --testPathPattern 'tests/contract' --testMatch '**/tests/**/*.test.ts'`

---

## Remaining Gaps (Not Yet Implemented)

| # | Gap | Priority | Effort | Notes |
|---|-----|----------|--------|-------|
| 8 | Concurrent Message Handling | HIGH | Medium | Race condition tests for text-processor |
| 9 | App.ts Bootstrap | MEDIUM | Medium | Module isolation needed for side-effect imports |
| 10 | Server.ts Shutdown | MEDIUM | Medium | Signal handler testing |
| 11 | HITL Full Cycle | MEDIUM | Large | LangGraph interrupt/resume with checkpointing |
| 12 | Checkpoint Backend | MEDIUM | Small | Factory function tests |
| 13 | Streaming Protocol (Py emitter) | MEDIUM | Small | Python-side NDJSON output validation |
