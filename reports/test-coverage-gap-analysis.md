# Test Coverage Gap Analysis

Date: 2026-06-21

## Executive Summary

The repository has a useful but uneven test base. The active TypeScript unit
suite passes and covers the main Telegram service layer at a moderate level,
but it does not yet provide the "new feature broke it" confidence bar described
in the goal.

The biggest risk is not only missing tests. It is that a large set of
`todoist-mcp` tests exists but is not wired into the current package scripts and
does not execute with the installed Jest-based toolchain. Python agent tests
also exist, but there is no package script or working local Python command in
this shell to run them consistently.

Recommended target state:

- Every push runs TypeScript unit tests, TypeScript integration tests, Python
  agent tests, build, lint, and the Todoist MCP test suite.
- Coverage gates fail the build for core routing, HITL, Telegram handlers,
  Todoist dispatch, and agent API routes.
- Each feature area has at least one contract/integration test across its
  public boundary, not only isolated unit tests.

## Current Test Inventory

### Package Scripts

`package.json` defines:

- `npm test`: Jest unit tests using `jest.config.js`.
- `npm run test:integration`: Jest integration tests using
  `jest.integration.config.js`.
- `npm run test:coverage`: Jest unit tests with coverage.
- `npm run build`: TypeScript compile.
- `npm run lint`: ESLint over `src/**/*.ts`.

There is no script for:

- Python tests under `tests/agents`.
- Todoist MCP tests under `todoist-mcp/**/*.test.ts`.
- A combined "pre-push" or CI command.

### Tests That Actually Run Under Current Jest Config

`npm test -- --listTests` reports 24 unit test files, all under `tests/unit`.
These cover:

- Webhook controller.
- Telegram bot service, handlers, processors, formatters, file/status/progress
  services.
- LangGraph agent client and Whisper service.
- Todoist REST wrapper and direct tool dispatcher.
- Validation helpers.

`npm run test:integration -- --listTests` reports 4 integration files:

- `tests/integration/function-calling.integration.test.ts`
- `tests/integration/live-services.integration.test.ts`
- `tests/integration/telegram-integration.test.ts`
- `tests/integration/webhook-pipeline.integration.test.ts`

The integration folder includes opt-in live tests gated by environment flags.

### Tests Present But Not Wired Into Normal Commands

`todoist-mcp` contains 65 test files, including broad coverage for Todoist tool
operations, snapshots, resolver utilities, retry behavior, token footprint, and
plugin configuration. These are not matched by `jest.config.js` because
`testMatch` is restricted to `**/tests/unit/**/*.test.ts`.

When invoked with an override, the suite still does not execute in this
workspace because the tests are written for a different toolchain and missing
dependencies:

- `vitest`
- `@doist/todoist-sdk`
- `@modelcontextprotocol/sdk`
- `gpt-tokenizer`

There are also ESM-style `.js` imports from TypeScript test files, for example
`./tool-execution-error.js`, that do not resolve under the current Jest/ts-jest
setup.

### Python Tests

Python tests exist in:

- `tests/agents/test_api.py`
- `tests/agents/test_jarvis.py`

They cover FastAPI route behavior, streaming progress output, config defaults,
Todoist retry classification, fake LangGraph agent flows, and compatibility
exports.

However, there is no npm script or documented repo command that reliably runs
them from the Node workflow. In this shell, both `python -m pytest tests/agents
-q` and `python3.13 -m pytest tests/agents -q` failed before test discovery
because pyenv does not expose those commands.

## Verification Run

Commands executed during this audit:

```bash
npm test -- --listTests
npm run test:integration -- --listTests
npm test -- --coverage --runInBand
python -m pytest tests/agents -q
python3.13 -m pytest tests/agents -q
npx jest --runInBand --testMatch '**/todoist-mcp/**/*.test.ts' todoist-mcp/tools/add-tasks.test.ts todoist-mcp/tool-helpers.test.ts todoist-mcp/utils/duration-parser.test.ts
```

Results:

- Unit tests passed: 24 suites, 122 tests.
- Unit coverage: 72.92% statements, 64.89% branches, 74.42% functions,
  73.86% lines.
- Python test command blocked by local Python command resolution.
- Todoist MCP ad-hoc test command failed before running any test cases because
  of runner/dependency/module-resolution mismatches.

## Coverage Findings

### High-Risk Gaps

1. Todoist MCP tests are present but effectively inert.

The `todoist-mcp` directory has the most extensive test-looking surface in the
repo, but it is neither included in `npm test` nor runnable with current
dependencies. Any feature in `todoist-mcp` can regress without the normal test
suite noticing.

Recommended fixes:

- Decide whether `todoist-mcp` is active production scope.
- If active, add a dedicated Vitest setup:
  - `vitest.config.ts`
  - `test:todoist-mcp`
  - required dev dependencies
  - CI/pre-push inclusion
- If inactive, move it out of the confidence boundary or archive its tests so
  they do not create a false sense of coverage.

2. Python agent test execution is not integrated.

The production path depends on the Python LangGraph API for `/invoke`,
`/resume`, HITL, and Todoist execution. These tests must be first-class in the
push gate.

Recommended fixes:

- Add a stable command such as `npm run test:agents` that calls a known Python
  executable or project virtualenv.
- Add Python dependency setup instructions and pin test dependencies.
- Include `tests/agents` in CI.
- Add coverage for the modular files under `agents/agent_api/app`, not only the
  compatibility aggregator.

3. `src/app.ts` and `src/server.ts` are untested deploy-critical code.

These files validate environment variables, construct services, register the
Telegram webhook, expose `/ping`, mount the webhook router, and handle shutdown.
They are not directly covered by current Jest tests.

Recommended tests:

- Missing required env var exits with an error and logs the missing key.
- Invalid `ALLOWED_TELEGRAM_USER_IDS` exits before service construction.
- Rich messages flag propagates to Telegram config.
- `/ping` returns `{ "status": "ok" }`.
- Webhook registration failures are logged without crashing the server.
- `SIGTERM` path logs shutdown.

4. HITL pending clarification storage is under-tested.

Coverage shows `pending-clarification.store.ts` at about 36.58% statements and
5.88% branches. This file owns the memory/Postgres bridge for resume behavior,
which is a correctness-sensitive feature.

Recommended tests:

- Memory store returns saved records before expiry.
- Memory store drops expired records.
- Memory store `clear` removes only the pending key.
- Store factory selects memory by default.
- Store factory selects Postgres when DSN exists.
- Store factory throws when `TELEGRAM_PENDING_STORE=postgres` is set without a
  DSN.
- Postgres store SQL behavior using a fake `Pool`:
  - lazy table creation happens once
  - `save` upserts records
  - `get` maps nullable columns correctly
  - expired pending rows are marked expired
  - `clear` updates status only for pending rows

5. Audio pipeline edge cases are thin.

`audio-processor.service.ts` has about 26.92% statement coverage. Existing
tests cover happy path and constructor config only.

Recommended tests:

- Empty or one-character transcription returns "No speech detected".
- Text processor failure returns transcription plus retry guidance.
- Oversized audio error maps to the user-facing size message.
- Unsupported format maps to the user-facing format message.
- Conversion unavailable and conversion failed map to distinct guidance.
- Download failure maps to retry guidance.
- Audio document variants include file name in all error messages.
- `processAudioDocument` includes processing time and passes transcribed text to
  the shared text processor.

6. Telegram message handlers have low branch coverage.

`message-handlers.ts` is about 38.09% statements and 40.17% branches. This is
where Telegram update variants become user-visible behavior.

Recommended tests:

- `handleVoice` success and `getFileUrl` failure.
- `handleAudio` delegates to the private audio file path.
- `handlePhoto` picks the largest photo, forwards caption and metadata, and
  handles missing photo arrays.
- `handleDocument` accepts audio MIME types and rejects non-audio documents.
- Unsupported sticker, video note, animation, and unknown message replies.
- Text progress reporter is completed as "Paused for clarification" when the
  last progress stage indicates pause/clarification.
- Error paths call the correct user-facing fallback and do not leak raw errors.

7. `MessageProcessorService` branch coverage is incomplete.

Current tests mostly spy on routing methods. They do not strongly assert the
constructed photo prompt or logger privacy behavior.

Recommended tests:

- `processPhotoMessage` builds the exact context message for captioned and
  uncaptioned photos.
- Missing dimensions/file size are omitted cleanly.
- `processAudioMessage` logs only truncated file URLs.
- `processMessage` propagates `logContext` for every message type.
- Unknown type logs warning and returns the documented fallback.

8. Legacy GPT function-calling processor remains weakly covered.

`function-calling.processor.ts` is about 40% statements. If this path is still
supported, it needs tests; if not, it should be explicitly deprecated or removed
from the regression surface.

Recommended tests if active:

- Direct response with no tool calls.
- Tool calls without a dispatcher return unavailable message.
- Unsupported tool calls are filtered and logged.
- Multiple supported tool calls execute and format results.
- Invalid JSON arguments do not crash logging.
- Dispatcher failure returns user-facing retry guidance.

9. Audio conversion utility is barely covered.

`audioConverter.ts` is about 16% statements. This utility sits below Whisper and
can fail because of binary availability, file format, size, and filesystem
behavior.

Recommended tests:

- Supported input format bypasses conversion when appropriate.
- Unsupported extension is rejected.
- Missing converter produces the expected actionable error.
- Conversion failure includes the documented message.
- Temporary files are cleaned up on success and failure.
- Large file handling is deterministic.

10. LangGraph API client branch coverage should be higher.

`langgraph-agent-client.service.ts` is reasonably covered at the statement level
but only about 65.95% branch coverage. This client owns `/invoke` vs `/resume`,
stream parsing, progress events, request IDs, and failure classification.

Recommended tests:

- `/invoke` request shape includes user and request context.
- `/resume` request shape includes thread ID and clarification reply.
- Streaming parser handles multiple progress events then final event.
- Streaming parser handles malformed JSON lines.
- Non-2xx HTTP responses include status/body in errors.
- Network failures produce actionable error messages.
- Interrupted response persists pending clarification.
- Completed resume clears pending clarification.

### Medium-Risk Gaps

1. `webhook.controller.ts` lacks some negative-path tests.

Coverage misses lines around secret validation/handling. Add tests for:

- Wrong secret returns expected status.
- Missing/empty secret behavior.
- Malformed Telegram update body.
- Bot service `handleUpdate` rejection is converted to a safe HTTP response.

2. `telegram-bot.service.ts` misses lifecycle branches.

Add tests for:

- Webhook URL normalization and secret usage.
- Authorized vs unauthorized users across text and non-text updates.
- Telegraf middleware/handler registration.
- `setupWebhook` failures.
- `handleUpdate` no-op or error behavior on malformed updates.

3. Formatter branch coverage is uneven.

`telegram-markdown.ts` has low branch coverage. Add table-driven escaping tests
for MarkdownV2 special characters, nested formatting, long messages, and
fallback formatting.

4. Live test boundaries need sharper docs and CI separation.

Live tests are correctly gated, but they should never be required for ordinary
pushes. Keep them in a separate nightly/manual lane and make offline contract
tests the mandatory push gate.

## Feature-to-Test Matrix

| Feature area | Current coverage | Missing coverage to add |
| --- | --- | --- |
| Telegram webhook | Unit + mocked integration | app/server wiring, bad secrets, malformed updates, bot failures |
| Telegram commands | Unit tests | authorization and end-to-end command dispatch through webhook |
| Text messages | Unit + mocked integration | progress pause status, resume/HITL end-to-end contract |
| Photo messages | Some routing tests | handler success/error paths, exact context prompt variants |
| Voice/audio messages | Thin unit tests | all transcription failure mappings, document variants, conversion utility |
| Unsupported media | Partial handler code | sticker/video note/animation/unknown assertions |
| LangGraph client | Unit tests | streaming error cases, request contracts, pending clarification lifecycle |
| Pending clarification | Very thin coverage | memory expiry, factory selection, Postgres SQL behavior |
| Python FastAPI agent | Python tests present | reliable command, modular route/service/graph coverage |
| Todoist REST via TypeScript | Good unit coverage | more HTTP error edge cases, live tests remain opt-in |
| Legacy GPT tools | Integration + low unit coverage | direct response, unsupported tools, dispatcher failures, invalid args |
| Todoist MCP plugin/tools | Many tests present but inert | runnable Vitest suite and required dependencies |

## Recommended Push Gate

Add a single local/CI command that runs the same checks developers trust before
shipping:

```bash
npm run build
npm run lint
npm test -- --runInBand --coverage
npm run test:integration -- --runInBand
npm run test:agents
npm run test:todoist-mcp
```

Then add coverage thresholds. A practical starting point:

- Global: 80% statements, 75% branches.
- `src/services/telegram/handlers`: 85% statements, 80% branches.
- `src/services/telegram/pending-clarification.store.ts`: 90% statements, 85%
  branches.
- `src/services/telegram/processors/audio-processor.service.ts`: 90%
  statements, 85% branches.
- `src/services/ai/langgraph-agent-client.service.ts`: 90% statements, 80%
  branches.
- `src/controllers/webhook.controller.ts`: 90% statements, 85% branches.

Raise thresholds gradually after filling the high-risk gaps. Avoid setting
global gates so high initially that they block useful work before the dormant
test suites are fixed.

## Suggested Implementation Plan

### Phase 1: Make Existing Tests Truthful

1. Add `test:agents` and document the expected Python executable/venv.
2. Add `test:todoist-mcp` with the correct runner, or explicitly mark
   `todoist-mcp` out of active scope.
3. Add a `test:all` or `check` command that runs build, lint, unit,
   integration, Python, and Todoist MCP tests.
4. Add CI or pre-push automation that runs the same command.

### Phase 2: Cover Deploy-Critical Boundaries

1. Add app/server wiring tests.
2. Add pending clarification store tests.
3. Add LangGraph client request/stream/error contract tests.
4. Add webhook negative-path tests.

### Phase 3: Fill User-Visible Telegram Behavior

1. Expand message handler tests for voice, audio, photo, document, and
   unsupported media.
2. Expand audio processor tests for all error mappings.
3. Add exact prompt/context tests for photo and audio transcription handoff.

### Phase 4: Retire or Strengthen Legacy Paths

1. Decide whether legacy GPT function-calling is active.
2. If active, add focused unit tests around tool-call handling.
3. If inactive, remove it from runtime and tests or document it as legacy.

## Definition of Done for New Features

For each new feature, require:

- Unit tests for new branches and error handling.
- A contract test at the public boundary the feature changes:
  - HTTP route
  - Telegram handler
  - LangGraph client/API
  - Todoist tool dispatcher
- Regression test for the bug or behavior that motivated the change.
- No drop in protected coverage thresholds.
- The full push gate passes locally and in CI.

This makes failures attributable: if the baseline suite is green and a new
feature adds tests for its own boundary, a later red build is far more likely to
point at the new feature or an intentional contract change rather than an
unknown pre-existing hole.
