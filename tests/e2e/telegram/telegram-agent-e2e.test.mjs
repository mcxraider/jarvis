import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  buildTelegramTextUpdate,
  checkEndpoint,
  createLogCursor,
  evaluateRunEvents,
  postTelegramUpdate,
  readNewLogEvents,
  resolveConfig,
  waitForCompletion,
} from './telegram-agent-e2e.mjs';

const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
const SHELL_RUNNER = path.join(TEST_DIR, 'run_telegram_e2e.sh');
const JAVASCRIPT_RUNNER = path.join(TEST_DIR, 'telegram-agent-e2e.mjs');
const REQUEST_ID = 'tg_update_9001';

function baseConfig(overrides = {}) {
  return resolveConfig(
    {
      JARVIS_TELEGRAM_E2E_USER_ID: '123456789',
      TELEGRAM_SECRET_TOKEN: 'secret',
      ...overrides,
    },
    { updateId: 9001 },
  );
}

function event(message, extra = {}, requestId = REQUEST_ID) {
  return { message, requestId, ...extra };
}

function successfulEvents() {
  return [
    event('telegram.webhook.received'),
    event('telegram.update.handling_started'),
    event('langgraph.stream.completed', { status: 'completed' }),
    event('text_processor.completed', { agentStatus: 'completed' }),
    event('telegram.reply.sent'),
    event('telegram.update.handling_completed'),
  ];
}

test('resolves configuration, aliases, marker prompts, and group chat IDs', () => {
  const config = resolveConfig(
    {
      JARVIS_CLI_USER_1_TELEGRAM_ID: '123456789',
      JARVIS_TELEGRAM_E2E_CHAT_ID: '-1001234567890',
      JARVIS_TELEGRAM_PROMPT: 'Return {marker}',
      JARVIS_TELEGRAM_E2E_TIMEOUT_MS: '9000',
      TELEGRAM_SECRET_TOKEN: 'secret',
      PORT: '3456',
    },
    { updateId: 9001 },
  );

  assert.equal(config.userId, 123456789);
  assert.equal(config.chatId, -1001234567890);
  assert.equal(config.baseUrl, 'http://127.0.0.1:3456');
  assert.equal(config.timeoutMs, 9000);
  assert.equal(config.prompt, 'Return JARVIS_E2E_9001');
  assert.equal(config.requestId, REQUEST_ID);
});

test('rejects missing and invalid configuration', () => {
  assert.throws(
    () => resolveConfig({ TELEGRAM_SECRET_TOKEN: 'secret' }, { updateId: 1 }),
    /JARVIS_TELEGRAM_E2E_USER_ID/,
  );
  assert.throws(
    () =>
      resolveConfig(
        { JARVIS_TELEGRAM_E2E_USER_ID: 'not-a-number', TELEGRAM_SECRET_TOKEN: 'secret' },
        { updateId: 1 },
      ),
    /positive integer/,
  );
  assert.throws(
    () => resolveConfig({ JARVIS_TELEGRAM_E2E_USER_ID: '123' }, { updateId: 1 }),
    /TELEGRAM_SECRET_TOKEN/,
  );
});

test('builds a realistic private Telegram text update', () => {
  const config = baseConfig();
  const update = buildTelegramTextUpdate(config, 1_700_000_000_000);

  assert.deepEqual(update, {
    update_id: 9001,
    message: {
      message_id: 9001,
      date: 1_700_000_000,
      chat: { id: 123456789, type: 'private' },
      from: { id: 123456789, is_bot: false, first_name: 'Jarvis E2E' },
      text: 'Reply exactly with JARVIS_E2E_9001',
    },
  });
});

test('posts to the encoded webhook URL and rejects non-2xx responses', async () => {
  const config = baseConfig({ TELEGRAM_SECRET_TOKEN: 'a/b' });
  const calls = [];
  const fetcher = async (...args) => {
    calls.push(args);
    return { ok: true, status: 200 };
  };

  await postTelegramUpdate(fetcher, config, buildTelegramTextUpdate(config));
  assert.equal(calls[0][0], 'http://127.0.0.1:3000/webhook/a%2Fb');
  assert.equal(calls[0][1].method, 'POST');
  assert.match(calls[0][1].body, /JARVIS_E2E_9001/);

  await assert.rejects(
    postTelegramUpdate(
      async () => ({ ok: false, status: 401 }),
      config,
      buildTelegramTextUpdate(config),
    ),
    /HTTP 401/,
  );
});

test('rejects unhealthy and unreachable health endpoints', async () => {
  await assert.rejects(
    checkEndpoint(async () => ({ ok: false, status: 503 }), 'http://test/health', 'Readiness'),
    /HTTP 503/,
  );
  await assert.rejects(
    checkEndpoint(async () => {
      throw new Error('connection refused');
    }, 'http://test/ping', 'Liveness'),
    /connection refused/,
  );
});

test('passes only a complete ordered event chain for the matching request', () => {
  const events = [
    ...successfulEvents().map((candidate) => ({ ...candidate, requestId: 'tg_update_other' })),
    ...successfulEvents(),
  ];
  assert.equal(evaluateRunEvents(events, REQUEST_ID).status, 'passed');

  const missingReply = successfulEvents().filter((candidate) => candidate.message !== 'telegram.reply.sent');
  assert.deepEqual(evaluateRunEvents(missingReply, REQUEST_ID), {
    status: 'pending',
    missing: 'telegram.reply.sent',
    observed: missingReply.map((candidate) => candidate.message),
  });
});

test('log cursor ignores historical entries and parses only newly appended JSON events', async () => {
  const temporaryDirectory = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'jarvis-e2e-'));
  const logFile = path.join(temporaryDirectory, 'app.log');
  try {
    await fs.promises.writeFile(
      logFile,
      `${JSON.stringify(event('telegram.webhook.received', {}, 'historical'))}\n`,
    );
    const cursor = await createLogCursor(logFile);
    const appended = event('telegram.reply.sent');
    await fs.promises.appendFile(logFile, `${JSON.stringify(appended)}\n`);

    assert.deepEqual(await readNewLogEvents(cursor), [appended]);
    assert.deepEqual(await readNewLogEvents(cursor), []);
  } finally {
    await fs.promises.rm(temporaryDirectory, { recursive: true, force: true });
  }
});

test('fails on authorization, application, model, processor, and delivery errors', () => {
  const failures = [
    event('telegram.update.denied'),
    event('telegram.webhook.background_failed'),
    event('telegram.message.failed'),
    event('langgraph.request.failed'),
    event('langgraph.stream.completed', { status: 'failed', agentError: 'provider failed' }),
    event('text_processor.completed', { agentStatus: 'interrupted' }),
  ];

  for (const failure of failures) {
    assert.equal(evaluateRunEvents([failure], REQUEST_ID).status, 'failed', failure.message);
  }
});

test('allows a stream failure when the non-streaming fallback succeeds', () => {
  const events = successfulEvents();
  events.splice(2, 0, event('langgraph.stream.failed', { failureKind: 'safe_to_retry' }));
  events[3] = event('langgraph.request.completed', { status: 'completed' });
  assert.equal(evaluateRunEvents(events, REQUEST_ID).status, 'passed');
});

test('times out when the asynchronous completion chain never arrives', async () => {
  let clock = 0;
  await assert.rejects(
    waitForCompletion({
      requestId: REQUEST_ID,
      timeoutMs: 10,
      readEvents: async () => [],
      now: () => clock,
      sleep: async (milliseconds) => {
        clock += milliseconds;
      },
      pollIntervalMs: 5,
    }),
    /Timed out.*telegram.webhook.received/,
  );
});

test('shell runner validates and selects explicit and automatic execution modes', () => {
  const syntax = spawnSync('bash', ['-n', SHELL_RUNNER], { encoding: 'utf8' });
  assert.equal(syntax.status, 0, syntax.stderr);

  const explicit = spawnSync(
    'bash',
    ['-c', 'source "$1"; JARVIS_TELEGRAM_E2E_MODE=compose select_execution_mode', 'bash', SHELL_RUNNER],
    { encoding: 'utf8' },
  );
  assert.equal(explicit.status, 0, explicit.stderr);
  assert.equal(explicit.stdout.trim(), 'compose');

  const automaticCompose = spawnSync(
    'bash',
    [
      '-c',
      'source "$1"; docker() { if [[ "$*" == *"ps --status running --services"* ]]; then printf "web\\n"; fi; }; select_execution_mode',
      'bash',
      SHELL_RUNNER,
    ],
    { encoding: 'utf8' },
  );
  assert.equal(automaticCompose.status, 0, automaticCompose.stderr);
  assert.equal(automaticCompose.stdout.trim(), 'compose');

  const automaticLocal = spawnSync(
    'bash',
    [
      '-c',
      'source "$1"; docker() { return 1; }; select_execution_mode',
      'bash',
      SHELL_RUNNER,
    ],
    { encoding: 'utf8' },
  );
  assert.equal(automaticLocal.status, 0, automaticLocal.stderr);
  assert.equal(automaticLocal.stdout.trim(), 'local');
});

test('self-contained runner executes when piped to Node like the Compose path', () => {
  const piped = spawnSync(process.execPath, ['--input-type=module', '-', '--run'], {
    cwd: os.tmpdir(),
    encoding: 'utf8',
    env: { JARVIS_TELEGRAM_E2E_USER_ID: '123' },
    input: fs.readFileSync(JAVASCRIPT_RUNNER, 'utf8'),
  });

  assert.equal(piped.status, 1);
  assert.match(piped.stderr, /FAIL: TELEGRAM_SECRET_TOKEN is required/);
});
