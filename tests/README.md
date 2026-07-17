# Testing Jarvis

This directory contains offline tests, mocked integration tests, and opt-in live tests for
Telegram, the Python LangGraph agent, and Todoist.

## Quick Start

Run the normal offline test suite:

```bash
npm test -- --runInBand
```

Run mocked integration tests and any explicitly enabled live tests:

```bash
npm run test:integration -- --runInBand
```

Run build and lint checks:

```bash
npm run build
npm run lint
```

## Test Types

### Simulate Telegram From the CLI

With the TypeScript server and Python agent running, inject a Telegram-shaped
text update at the real webhook entry point and wait for the correlated model
response to be accepted by Telegram:

```bash
./tests/e2e/telegram/run_telegram_e2e.sh
```

Set `JARVIS_TELEGRAM_E2E_USER_ID` when testing another configured identity and
`JARVIS_TELEGRAM_E2E_CHAT_ID` when the destination differs from that user. The
selected identity follows the normal authorization and per-user integration
resolution path. See `tests/e2e/telegram/README.md` for deployment, local, and
prompt configuration.

This simulates Telegram's JSON webhook payload from the HTTP ingress onward. It
does not traverse Telegram's servers: Telegram does not allow a bot or local
CLI to forge an incoming message from another user. The outbound response does
use the real Bot API and is correlated through the existing async application
log before the command reports success.

### Offline Unit Tests

Command:

```bash
npm test -- --runInBand
```

These tests should not call Telegram, OpenAI, or Todoist. They cover routing,
validation, Todoist tool schemas, dispatcher routing, and Todoist REST request
serialization with mocked `fetch`.

### Mocked Integration Tests

Command:

```bash
npm run test:integration -- --runInBand
```

These tests exercise larger code paths with fake external services:

- Legacy GPT function-calling flow with a fake OpenAI client and fake dispatcher.
- Webhook text update flow with mocked message processing and reply behavior.

### Live Telegram Smoke Tests

Command:

```bash
RUN_INTEGRATION_TESTS=true npm run test:integration -- --runInBand
```

Required environment variables:

```bash
BOT_TOKEN=...
NGROK_URL=https://your-ngrok-url.ngrok-free.app
TELEGRAM_SECRET_TOKEN=...
TEST_CHAT_ID=...
```

These tests call the real Telegram Bot API to get bot info and send a test
message to `TEST_CHAT_ID`.

To manually send the rich-message formatting fixtures as 11 separate messages:

```bash
npm run test:telegram-rich
```

This script reads `BOT_TOKEN` and `TEST_CHAT_ID` from the environment or `.env`.

To preview every custom emoji in the Telegram `AIActions` set:

```bash
npm run test:telegram-rich-emojis
```

The script fetches the current sticker set at runtime and streams each emoji
inside a thinking draft, one at a time, with a five-second pause between
previews. Set `TEST_TELEGRAM_EMOJI_SET` to test a different custom emoji set.

### Live Todoist CRUD Tests

Command:

```bash
RUN_LIVE_TODOIST_TESTS=true npm run test:integration -- --runInBand
```

Required environment variable:

```bash
TODOIST_API_KEY=...
```

These tests create, fetch, update, list, complete, and delete real Todoist
tasks. Test tasks use timestamped names and the label `jarvis-test`.

### Live LangGraph + Todoist Pipeline Tests

Command:

```bash
RUN_LIVE_LANGGRAPH_TODOIST_TESTS=true npm run test:integration -- --runInBand
```

Required environment variables:

```bash
LANGGRAPH_AGENT_URL=http://localhost:8000
TODOIST_API_KEY=...
```

These tests call the real Python LangGraph API through `MessageProcessorService`,
expect a Todoist tool call to create a task, then verify the task exists in Todoist.

### Live Webhook Tests

Start the backend first:

```bash
npm run dev
```

In another terminal, run:

```bash
RUN_LIVE_WEBHOOK_TESTS=true npm run test:integration -- --runInBand
```

Required environment variables:

```bash
NGROK_URL=https://your-ngrok-url.ngrok-free.app
TELEGRAM_SECRET_TOKEN=...
TODOIST_API_KEY=...
TEST_CHAT_ID=...
TEST_USER_ID=...
```

This posts a Telegram-shaped update to:

```text
${NGROK_URL}/webhook/${TELEGRAM_SECRET_TOKEN}
```

The test expects HTTP `200`, then checks Todoist for the created task.

## Test Logs

Test runs write sanitized output to:

```text
logs/test-runs/
```

Each run directory contains:

- `summary.md` - start here for a readable summary.
- `events.jsonl` - step-by-step structured events.
- `artifacts/*.json` - larger sanitized payloads when a test writes them.

Secrets and private IDs are redacted before writing logs. The logbase is ignored
by git and should stay local.

## Cleanup

Live Todoist tests attempt automatic cleanup with `try/finally`. If a test is
interrupted, manually search Todoist for:

```text
jarvis-test
```

Then delete or complete any leftover timestamped test tasks.
