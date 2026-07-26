import fs from 'node:fs';
import path from 'node:path';

const DEFAULT_TIMEOUT_MS = 180_000;
const DEFAULT_POLL_INTERVAL_MS = 250;
const HTTP_TIMEOUT_MS = 5_000;
const MAX_TELEGRAM_ID = 2_147_483_647;

const REQUIRED_SUCCESS_EVENTS = [
  'telegram.webhook.received',
  'telegram.update.handling_started',
  'langgraph.completed',
  'text_processor.completed',
  'telegram.reply.sent',
  'telegram.update.handling_completed',
];

const TERMINAL_FAILURE_EVENTS = new Set([
  'telegram.webhook.background_failed',
  'telegram.update.denied',
  'telegram.update.handling_failed',
  'telegram.message.failed',
  'telegram.bot.error',
  'telegram.bot.error_reply_failed',
  'telegram.send_message.failed',
  'text_processor.failed',
  'langgraph.request.failed',
]);

function parseEnvFile(contents) {
  const parsed = {};
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!match) continue;
    let value = match[2].trim();
    if (
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    } else {
      value = value.replace(/\s+#.*$/, '').trim();
    }
    parsed[match[1]] = value;
  }
  return parsed;
}

export function loadEnvironment(baseEnv = process.env, envFile = path.join(process.cwd(), '.env')) {
  let fromFile = {};
  try {
    fromFile = parseEnvFile(fs.readFileSync(envFile, 'utf8'));
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  return { ...fromFile, ...baseEnv };
}

function parsePositiveInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer.`);
  }
  return parsed;
}

function parseChatId(value, name) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed === 0) {
    throw new Error(`${name} must be a non-zero integer.`);
  }
  return parsed;
}

function createUpdateId(nowMs = Date.now(), processId = process.pid) {
  const candidate = Math.abs((nowMs + processId) % MAX_TELEGRAM_ID);
  return candidate || 1;
}

function promptWithMarker(configuredPrompt, marker) {
  if (!configuredPrompt?.trim()) return `Reply exactly with ${marker}`;
  if (configuredPrompt.includes('{marker}')) {
    return configuredPrompt.replaceAll('{marker}', marker);
  }
  return `${configuredPrompt.trim()}\n\nEnd your response with exactly: ${marker}`;
}

export function resolveConfig(env = process.env, runtime = {}) {
  const userIdValue = env.JARVIS_TELEGRAM_E2E_USER_ID || env.JARVIS_CLI_USER_1_TELEGRAM_ID;
  if (!userIdValue) {
    throw new Error(
      'Set JARVIS_TELEGRAM_E2E_USER_ID or JARVIS_CLI_USER_1_TELEGRAM_ID to an authorized Telegram user.',
    );
  }
  if (!env.TELEGRAM_SECRET_TOKEN) {
    throw new Error('TELEGRAM_SECRET_TOKEN is required.');
  }

  const userId = parsePositiveInteger(userIdValue, 'Telegram E2E user ID');
  const chatId = env.JARVIS_TELEGRAM_E2E_CHAT_ID
    ? parseChatId(env.JARVIS_TELEGRAM_E2E_CHAT_ID, 'Telegram E2E chat ID')
    : userId;
  const timeoutMs = env.JARVIS_TELEGRAM_E2E_TIMEOUT_MS
    ? parsePositiveInteger(env.JARVIS_TELEGRAM_E2E_TIMEOUT_MS, 'JARVIS_TELEGRAM_E2E_TIMEOUT_MS')
    : DEFAULT_TIMEOUT_MS;
  const updateId = runtime.updateId ?? createUpdateId(runtime.nowMs, runtime.processId);
  const marker = `JARVIS_E2E_${updateId}`;
  const baseUrl = (
    env.JARVIS_TELEGRAM_BASE_URL || `http://127.0.0.1:${env.PORT || '3000'}`
  ).replace(/\/+$/, '');
  const logDirectory = env.JARVIS_LOG_DIR || path.join(process.cwd(), 'logs');

  return {
    userId,
    chatId,
    timeoutMs,
    updateId,
    messageId: updateId,
    marker,
    prompt: promptWithMarker(env.JARVIS_TELEGRAM_PROMPT, marker),
    baseUrl,
    secret: env.TELEGRAM_SECRET_TOKEN,
    logFile: env.JARVIS_TELEGRAM_E2E_LOG_FILE || path.join(logDirectory, 'app.log'),
    requestId: `tg_update_${updateId}`,
  };
}

export function buildTelegramTextUpdate(config, nowMs = Date.now()) {
  return {
    update_id: config.updateId,
    message: {
      message_id: config.messageId,
      date: Math.floor(nowMs / 1000),
      chat: { id: config.chatId, type: 'private' },
      from: {
        id: config.userId,
        is_bot: false,
        first_name: 'Jarvis E2E',
      },
      text: config.prompt,
    },
  };
}

async function fetchWithTimeout(fetcher, url, options = {}, timeoutMs = HTTP_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetcher(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) throw new Error(`Request timed out: ${url}`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function checkEndpoint(fetcher, url, label) {
  let response;
  try {
    response = await fetchWithTimeout(fetcher, url);
  } catch (error) {
    throw new Error(`${label} is unreachable at ${url}: ${error.message}`);
  }
  if (!response.ok) {
    throw new Error(`${label} failed at ${url}: HTTP ${response.status}.`);
  }
}

export async function postTelegramUpdate(fetcher, config, update) {
  const url = `${config.baseUrl}/webhook/${encodeURIComponent(config.secret)}`;
  let response;
  try {
    response = await fetchWithTimeout(fetcher, url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(update),
    });
  } catch (error) {
    throw new Error(`Telegram webhook request failed: ${error.message}`);
  }
  if (!response.ok) {
    throw new Error(`Telegram webhook rejected update ${config.updateId}: HTTP ${response.status}.`);
  }
}

function relevantEvents(events, requestId) {
  return events.filter((event) => event?.requestId === requestId && typeof event.message === 'string');
}

function modelCompletion(event) {
  return event.message === 'langgraph.stream.completed' || event.message === 'langgraph.request.completed';
}

function hasAgentError(event) {
  return event.agentError !== undefined && event.agentError !== null && event.agentError !== '';
}

export function evaluateRunEvents(events, requestId) {
  const relevant = relevantEvents(events, requestId);
  for (const event of relevant) {
    if (TERMINAL_FAILURE_EVENTS.has(event.message)) {
      return { status: 'failed', reason: `Terminal event: ${event.message}` };
    }
    if (modelCompletion(event) && (event.status !== 'completed' || hasAgentError(event))) {
      return {
        status: 'failed',
        reason: `Model completed unsuccessfully: status=${event.status ?? 'unknown'}`,
      };
    }
    if (event.message === 'text_processor.completed' && event.agentStatus !== 'completed') {
      return {
        status: 'failed',
        reason: `Text processor completed with agentStatus=${event.agentStatus ?? 'unknown'}`,
      };
    }
  }

  const positions = [];
  for (const required of REQUIRED_SUCCESS_EVENTS) {
    const start = positions.length ? positions[positions.length - 1] + 1 : 0;
    const index = relevant.findIndex((event, candidateIndex) => {
      if (candidateIndex < start) return false;
      return required === 'langgraph.completed' ? modelCompletion(event) : event.message === required;
    });
    if (index === -1) {
      return {
        status: 'pending',
        missing: required,
        observed: relevant.map((event) => event.message),
      };
    }
    positions.push(index);
  }

  return { status: 'passed', observed: relevant.map((event) => event.message) };
}

export async function createLogCursor(logFile) {
  try {
    const stat = await fs.promises.stat(logFile);
    return { logFile, offset: stat.size, inode: stat.ino, remainder: '' };
  } catch (error) {
    if (error?.code === 'ENOENT') {
      throw new Error(`Application log not found at ${logFile}.`);
    }
    throw error;
  }
}

export async function readNewLogEvents(cursor) {
  const stat = await fs.promises.stat(cursor.logFile);
  if (cursor.inode !== stat.ino || stat.size < cursor.offset) {
    cursor.offset = 0;
    cursor.inode = stat.ino;
    cursor.remainder = '';
  }
  if (stat.size === cursor.offset) return [];

  const length = stat.size - cursor.offset;
  const buffer = Buffer.alloc(length);
  const handle = await fs.promises.open(cursor.logFile, 'r');
  let bytesRead = 0;
  try {
    ({ bytesRead } = await handle.read(buffer, 0, length, cursor.offset));
  } finally {
    await handle.close();
  }
  cursor.offset += bytesRead;
  if (bytesRead === 0) return [];

  const lines = `${cursor.remainder}${buffer.subarray(0, bytesRead).toString('utf8')}`.split(/\r?\n/);
  cursor.remainder = lines.pop() || '';
  const events = [];
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      events.push(JSON.parse(line));
    } catch {
      // app.log is JSON, but ignore a partial or unrelated non-JSON line defensively.
    }
  }
  return events;
}

const defaultSleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

export async function waitForCompletion({
  requestId,
  timeoutMs,
  readEvents,
  now = Date.now,
  sleep = defaultSleep,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}) {
  const startedAt = now();
  const events = [];
  let evaluation = evaluateRunEvents(events, requestId);
  while (now() - startedAt < timeoutMs) {
    events.push(...(await readEvents()));
    evaluation = evaluateRunEvents(events, requestId);
    if (evaluation.status === 'passed') return evaluation;
    if (evaluation.status === 'failed') throw new Error(evaluation.reason);
    await sleep(pollIntervalMs);
  }
  throw new Error(
    `Timed out after ${timeoutMs}ms waiting for ${evaluation.missing ?? 'completion'} ` +
      `for ${requestId}.`,
  );
}

export async function runTelegramE2E({
  env = loadEnvironment(),
  fetcher = globalThis.fetch,
  now = Date.now,
} = {}) {
  if (typeof fetcher !== 'function') throw new Error('This test requires Node.js 18 or newer.');
  const config = resolveConfig(env);
  const startedAt = now();

  process.stdout.write(`Telegram E2E request: ${config.requestId}\n`);
  process.stdout.write(`Target chat: ${config.chatId}\n`);
  process.stdout.write(`Expected Telegram marker: ${config.marker}\n`);

  await checkEndpoint(fetcher, `${config.baseUrl}/ping`, 'Jarvis liveness check');
  await checkEndpoint(fetcher, `${config.baseUrl}/health`, 'Jarvis readiness check');
  const cursor = await createLogCursor(config.logFile);
  const update = buildTelegramTextUpdate(config, now());
  await postTelegramUpdate(fetcher, config, update);
  await waitForCompletion({
    requestId: config.requestId,
    timeoutMs: config.timeoutMs,
    readEvents: () => readNewLogEvents(cursor),
    now,
  });

  const durationMs = now() - startedAt;
  process.stdout.write(`PASS: ${config.requestId} completed in ${durationMs}ms.\n`);
  process.stdout.write(`Confirm ${config.marker} is visible in Telegram chat ${config.chatId}.\n`);
  return { config, durationMs };
}

async function main() {
  try {
    await runTelegramE2E();
  } catch (error) {
    process.stderr.write(`FAIL: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  }
}

if (process.argv.includes('--run')) {
  await main();
}
