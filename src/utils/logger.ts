// Thin, non-blocking logging facade. Winston and all log I/O live in log-worker.ts.

import fs from 'fs';
import path from 'path';
import { Worker } from 'worker_threads';
import { redactValue } from './log-redact';

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

interface LogMessage {
  type: 'log';
  id: number;
  level: LogLevel;
  message: string;
  meta: Record<string, unknown>;
  timestamp: string;
}

interface QueuedLog {
  event: LogMessage;
  bytes: number;
}

interface WorkerAck {
  type: 'written' | 'flushed' | 'shutdown_complete';
  id?: number;
  stats?: WorkerStats;
  failed?: boolean;
}

interface WorkerStats {
  last_write_time?: string;
  last_write_failure_time?: string;
  writes_failed: number;
  flush_count: number;
  avg_flush_duration_ms: number;
}

export interface LoggerStats {
  queue_depth: number;
  queue_bytes_approx: number;
  queue_high_water: number;
  events_accepted: number;
  events_dropped: Record<LogLevel, number>;
  worker_alive: boolean;
  worker_restarts: number;
  last_write_time?: string;
  last_write_failure_time?: string;
  writes_failed: number;
  flush_count: number;
  avg_flush_duration_ms: number;
}

const MAX_QUEUE_EVENTS = 500;
const MAX_QUEUE_BYTES = 2 * 1024 * 1024;
const MAX_EVENT_BYTES = 64_000;
const MAX_STRING_LENGTH = 8_000;
const MAX_DEPTH = 8;
const MAX_FIELDS = 50;
const QUEUE_STATS_INTERVAL_MS = 60_000;
const SINGAPORE_TIME_ZONE = 'Asia/Singapore';
const singaporeTimestampFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: SINGAPORE_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

let worker: Worker | undefined;
let stderrOnly = false;
let accepting = true;
let intentionalShutdown = false;
let workerRestarts = 0;
let workerFailureHandled = false;
let nextEventId = 1;
let nextControlId = 1;
let pending: QueuedLog[] = [];
let inFlight: QueuedLog | undefined;
let pendingBytes = 0;
let accepted = 0;
const droppedByLevel: Record<LogLevel, number> = { debug: 0, info: 0, warn: 0, error: 0 };
let queueHighWater = 0;
let lastReportedDrops = 0;
let latestWorkerStats: WorkerStats = {
  writes_failed: 0,
  flush_count: 0,
  avg_flush_duration_ms: 0,
};
const controlWaiters = new Map<
  number,
  {
    type: WorkerAck['type'];
    resolve: () => void;
    reject: (error: Error) => void;
    timer: NodeJS.Timeout;
  }
>();
const drainWaiters = new Set<() => void>();

export function formatSingaporeLogTimestamp(date = new Date()): string {
  return singaporeTimestampFormatter.format(date).replace(',', '');
}

export interface LogContext {
  requestId?: string;
  updateId?: number;
  userId?: number | string;
  chatId?: number | string;
  messageId?: number;
  messageType?: string;
  threadId?: string;
  telegramUsername?: string;
  telegramFirstName?: string;
}

export function createRequestId(prefix = 'req'): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export function truncateForLog(value: string | undefined, maxLength = 80): string | undefined {
  if (!value) return value;
  return value.length > maxLength ? `${value.slice(0, maxLength)}...` : value;
}

function normalizeValue(value: unknown, seen: WeakSet<object>, depth = 0): unknown {
  if (depth > MAX_DEPTH) return '[Truncated: max depth]';

  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
      stack: value.stack,
    };
  }

  if (
    value === null ||
    typeof value === 'undefined' ||
    typeof value === 'number' ||
    typeof value === 'boolean'
  ) {
    return value;
  }

  if (typeof value === 'string') {
    if (value.length > MAX_STRING_LENGTH) {
      return value.slice(0, MAX_STRING_LENGTH) + `...[truncated, original_length=${value.length}]`;
    }
    return value;
  }

  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'function') return `[Function ${value.name || 'anonymous'}]`;
  if (typeof value === 'symbol') return value.toString();

  if (typeof value === 'object') {
    if (seen.has(value)) return '[Circular]';
    seen.add(value);

    if (Array.isArray(value)) {
      return value.map((item) => normalizeValue(item, seen, depth + 1));
    }

    if (value instanceof Date) return value.toISOString();
    if (Buffer.isBuffer(value)) return `[Buffer ${value.length} bytes]`;

    const entries = Object.entries(value);
    const normalized: Record<string, unknown> = {};
    const limit = Math.min(entries.length, MAX_FIELDS);
    for (let i = 0; i < limit; i++) {
      const [key, item] = entries[i];
      try {
        normalized[key] = normalizeValue(item, seen, depth + 1);
      } catch {
        normalized[key] = '[Unserializable]';
      }
    }
    if (entries.length > MAX_FIELDS) {
      normalized._truncated = { original_keys: entries.length };
    }
    return normalized;
  }

  return String(value);
}

function normalizeMeta(meta: unknown): Record<string, unknown> {
  if (!meta || typeof meta !== 'object') {
    return meta === undefined ? {} : { value: normalizeValue(meta, new WeakSet()) };
  }
  return normalizeValue(meta, new WeakSet()) as Record<string, unknown>;
}

function workerLocation(): { filename: string; execArgv?: string[] } {
  const compiledWorker = path.join(__dirname, 'log-worker.js');
  if (fs.existsSync(compiledWorker)) return { filename: compiledWorker };

  return {
    filename: path.join(__dirname, 'log-worker.ts'),
    execArgv: ['-r', require.resolve('ts-node/register')],
  };
}

function rejectControlWaiters(error: Error): void {
  for (const waiter of controlWaiters.values()) {
    clearTimeout(waiter.timer);
    waiter.reject(error);
  }
  controlWaiters.clear();
}

function startWorker(): Worker | undefined {
  if (worker || stderrOnly) return worker;

  try {
    const location = workerLocation();
    workerFailureHandled = false;
    worker = new Worker(location.filename, { execArgv: location.execArgv });
    worker.on('message', (message: WorkerAck) => {
      if (message.stats) latestWorkerStats = message.stats;
      if (message.type === 'written') {
        const written = inFlight;
        if (!written || message.id !== written.event.id) return;
        pendingBytes -= written.bytes;
        inFlight = undefined;
        dispatchNext();
        notifyDrainWaiters();
        return;
      }

      if (message.id === undefined) return;
      const waiter = controlWaiters.get(message.id);
      if (!waiter || waiter.type !== message.type) return;
      clearTimeout(waiter.timer);
      controlWaiters.delete(message.id);
      waiter.resolve();
    });
    worker.on('error', (error) => {
      process.stderr.write(`logger.worker.error ${error.message}\n`);
    });
    worker.on('exit', (code) => {
      worker = undefined;
      if (intentionalShutdown && code === 0) return;
      handleWorkerFailure(code);
    });
  } catch (error) {
    handleWorkerFailure(undefined, error);
  }

  return worker;
}

function getWorker(): Worker | undefined {
  return worker ?? startWorker();
}

function handleWorkerFailure(code?: number, cause?: unknown): void {
  if (workerFailureHandled) return;
  workerFailureHandled = true;
  worker = undefined;
  rejectControlWaiters(new Error(`Logger worker exited${code === undefined ? '' : ` (${code})`}`));

  if (inFlight) {
    pending.unshift(inFlight);
    inFlight = undefined;
  }

  if (accepting && workerRestarts < 1) {
    workerRestarts += 1;
    process.stderr.write(`logger.worker.restart attempt=${workerRestarts}\n`);
    startWorker();
    dispatchNext();
    return;
  }

  stderrOnly = true;
  process.stderr.write(
    `logger.worker.fallback code=${code ?? 'start_failed'} cause=${cause ? String(cause) : 'unknown'}\n`,
  );
  drainToFallback();
  notifyDrainWaiters();
}

function queueDepth(): number {
  return pending.length + (inFlight ? 1 : 0);
}

function drainToFallback(): void {
  if (inFlight) writeFallback(inFlight.event);
  pending.forEach(({ event }) => writeFallback(event));
  pending = [];
  inFlight = undefined;
  pendingBytes = 0;
}

function dispatchNext(): void {
  if (inFlight || pending.length === 0) return;
  const activeWorker = getWorker();
  if (!activeWorker) {
    drainToFallback();
    return;
  }

  inFlight = pending.shift();
  if (!inFlight) return;

  try {
    activeWorker.postMessage(inFlight.event);
  } catch {
    writeFallback(inFlight.event);
    pendingBytes -= inFlight.bytes;
    inFlight = undefined;
    dispatchNext();
  }
}

function notifyDrainWaiters(): void {
  if (queueDepth() !== 0) return;
  for (const resolve of drainWaiters) resolve();
  drainWaiters.clear();
}

function recordDrop(level: LogLevel): void {
  droppedByLevel[level] += 1;
}

function totalDrops(): number {
  return Object.values(droppedByLevel).reduce((total, count) => total + count, 0);
}

function hasCapacity(bytes: number): boolean {
  return queueDepth() < MAX_QUEUE_EVENTS && pendingBytes + bytes <= MAX_QUEUE_BYTES;
}

function admit(queued: QueuedLog): boolean {
  if (
    !hasCapacity(queued.bytes) &&
    (queued.event.level === 'warn' || queued.event.level === 'error')
  ) {
    const debugIndex = pending.findIndex(({ event }) => event.level === 'debug');
    if (debugIndex >= 0) {
      const [evicted] = pending.splice(debugIndex, 1);
      pendingBytes -= evicted.bytes;
      recordDrop('debug');
    }
  }

  if (!hasCapacity(queued.bytes)) {
    recordDrop(queued.event.level);
    return false;
  }

  pending.push(queued);
  pendingBytes += queued.bytes;
  accepted += 1;
  queueHighWater = Math.max(queueHighWater, queueDepth());
  dispatchNext();
  return true;
}

function writeFallback(event: LogMessage): void {
  const redacted = { ...event, meta: redactValue(event.meta) as Record<string, unknown> };
  process.stderr.write(`${JSON.stringify(redacted)}\n`);
}

function log(level: LogLevel, message: string, meta?: unknown): AsyncLogger {
  const event: LogMessage = {
    type: 'log',
    id: nextEventId++,
    level,
    message: String(message),
    meta: normalizeMeta(meta),
    timestamp: formatSingaporeLogTimestamp(),
  };
  if (!accepting) {
    recordDrop(level);
    return logger;
  }
  if (stderrOnly) {
    writeFallback(event);
    return logger;
  }

  let bytes = Buffer.byteLength(JSON.stringify(event), 'utf8');
  if (bytes > MAX_EVENT_BYTES) {
    event.meta = { _oversized: true, original_bytes: bytes, message: event.message };
    bytes = Buffer.byteLength(JSON.stringify(event), 'utf8');
  }
  admit({ event, bytes });
  return logger;
}

export interface AsyncLogger {
  debug(message: string, meta?: unknown): AsyncLogger;
  info(message: string, meta?: unknown): AsyncLogger;
  warn(message: string, meta?: unknown): AsyncLogger;
  error(message: string, meta?: unknown): AsyncLogger;
}

export const logger: AsyncLogger = {
  debug: (message, meta) => log('debug', message, meta),
  info: (message, meta) => log('info', message, meta),
  warn: (message, meta) => log('warn', message, meta),
  error: (message, meta) => log('error', message, meta),
};

export function getLoggerStats(): LoggerStats {
  return {
    queue_depth: queueDepth(),
    queue_bytes_approx: pendingBytes,
    queue_high_water: queueHighWater,
    events_accepted: accepted,
    events_dropped: { ...droppedByLevel },
    worker_alive: Boolean(worker) && !stderrOnly,
    worker_restarts: workerRestarts,
    ...latestWorkerStats,
  };
}

function waitForDrain(timeout: number): Promise<void> {
  if (queueDepth() === 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      drainWaiters.delete(onDrained);
      reject(new Error(`Logger queue drain timed out after ${timeout}ms`));
    }, timeout);
    const onDrained = () => {
      clearTimeout(timer);
      resolve();
    };
    drainWaiters.add(onDrained);
  });
}

function requestControl(
  type: 'flush' | 'shutdown',
  responseType: 'flushed' | 'shutdown_complete',
  timeout: number,
): Promise<void> {
  const activeWorker = worker;
  if (!activeWorker || stderrOnly) return Promise.resolve();
  const id = nextControlId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      controlWaiters.delete(id);
      reject(new Error(`Logger ${type} timed out after ${timeout}ms`));
    }, timeout);
    controlWaiters.set(id, { type: responseType, resolve, reject, timer });
    activeWorker.postMessage({ type, id });
  });
}

export async function flushLogger(timeout = 3000): Promise<void> {
  const startedAt = Date.now();
  await waitForDrain(timeout);
  const remaining = Math.max(1, timeout - (Date.now() - startedAt));
  await requestControl('flush', 'flushed', remaining);
}

export async function shutdownLogger(timeout = 3000): Promise<void> {
  if (!accepting && intentionalShutdown) return;
  accepting = false;
  await flushLogger(timeout);
  intentionalShutdown = true;
  const activeWorker = worker;
  const exitPromise = activeWorker
    ? new Promise<void>((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error(`Logger worker exit timed out after ${timeout}ms`)),
          timeout,
        );
        activeWorker.once('exit', () => {
          clearTimeout(timer);
          resolve();
        });
      })
    : Promise.resolve();
  await Promise.all([requestControl('shutdown', 'shutdown_complete', timeout), exitPromise]);
}

const queueStatsTimer = setInterval(() => {
  const drops = totalDrops();
  if (drops === lastReportedDrops) return;
  worker?.postMessage({
    type: 'queue_stats',
    accepted,
    droppedByLevel: { ...droppedByLevel },
    queueHighWater,
  });
  lastReportedDrops = drops;
}, QUEUE_STATS_INTERVAL_MS);
queueStatsTimer.unref();

/** @internal — test-only access to internals for crash/restart simulation */
export const _testInternals = {
  getWorker: () => worker,
  isStderrOnly: () => stderrOnly,
  getWorkerRestarts: () => workerRestarts,
};
