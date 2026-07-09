import fs from 'fs';
import path from 'path';
import { parentPort } from 'worker_threads';
import winston from 'winston';
import { SENSITIVE_KEY_PATTERN, PRIVATE_ID_KEY_PATTERN, redactValue } from './log-redact';

type LogLevel = 'debug' | 'info' | 'warn' | 'error';

interface LogMessage {
  type: 'log';
  id: number;
  level: LogLevel;
  message: string;
  meta: Record<string, unknown>;
  timestamp: string;
}

interface ShutdownMessage {
  type: 'shutdown';
  id: number;
}

interface FlushMessage {
  type: 'flush';
  id: number;
}

interface QueueStatsMessage {
  type: 'queue_stats';
  accepted: number;
  droppedByLevel: Record<LogLevel, number>;
  queueHighWater: number;
}

interface WorkerStats {
  last_write_time?: string;
  last_write_failure_time?: string;
  writes_failed: number;
  flush_count: number;
  avg_flush_duration_ms: number;
}

const LOG_DIR = path.join(process.cwd(), 'logs');
const LOG_LEVEL =
  process.env.LOG_LEVEL || (process.env.NODE_ENV === 'production' ? 'info' : 'debug');
const LOG_FORMAT =
  process.env.LOG_FORMAT || (process.env.NODE_ENV === 'production' ? 'json' : 'pretty');
const READABLE_CONTEXT_KEYS = [
  'requestId',
  'updateId',
  'userId',
  'chatId',
  'messageId',
  'messageType',
  'threadId',
];
const stats: WorkerStats = {
  writes_failed: 0,
  flush_count: 0,
  avg_flush_duration_ms: 0,
};
let totalFlushDurationMs = 0;

const redactionFormat = winston.format((info) => {
  Object.entries(info).forEach(([key, value]) => {
    info[key] = redactValue(value, key);
  });
  return info;
});

function formatReadableValue(value: unknown): string {
  if (value === undefined) return 'undefined';
  if (value === null || typeof value !== 'object') return String(value);
  return JSON.stringify(value, null, 2);
}

function indentMultiline(value: string, spaces = 4): string {
  const padding = ' '.repeat(spaces);
  return value
    .split('\n')
    .map((line) => `${padding}${line}`)
    .join('\n');
}

const readableFileFormat = winston.format.printf((info) => {
  const { timestamp, level, message, stack, ...metadata } = info;
  const metadataRecord = metadata as Record<string, unknown>;
  const contextLines = READABLE_CONTEXT_KEYS.filter((key) => metadataRecord[key] !== undefined).map(
    (key) => `  ${key}: ${formatReadableValue(metadataRecord[key])}`,
  );
  const remainingMetadata = Object.fromEntries(
    Object.entries(metadataRecord).filter(([key]) => !READABLE_CONTEXT_KEYS.includes(key)),
  );
  const metadataLines =
    Object.keys(remainingMetadata).length > 0
      ? ['  details:', indentMultiline(JSON.stringify(remainingMetadata, null, 2))]
      : [];
  const stackLines = typeof stack === 'string' ? ['  stack:', indentMultiline(stack)] : [];

  return [
    `[${timestamp}] ${String(level).toUpperCase()} ${message}`,
    ...contextLines,
    ...metadataLines,
    ...stackLines,
  ].join('\n');
});

const prettyConsoleFormat = winston.format.printf((info) => {
  const { timestamp, level, message, ...metadata } = info;
  const metadataText = Object.keys(metadata).length > 0 ? ` ${JSON.stringify(metadata)}` : '';
  return `${timestamp} ${level.padEnd(5)} ${message}${metadataText}`;
});

fs.mkdirSync(LOG_DIR, { recursive: true });

const timestampFormat = winston.format((info) => {
  if (!info.timestamp) info.timestamp = new Date().toISOString();
  return info;
})();
const jsonFileFormat = winston.format.combine(
  timestampFormat,
  redactionFormat(),
  winston.format.json(),
);
const humanReadableFileFormat = winston.format.combine(
  timestampFormat,
  redactionFormat(),
  readableFileFormat,
);

const logger = winston.createLogger({
  level: LOG_LEVEL,
  format: winston.format.combine(
    timestampFormat,
    winston.format.errors({ stack: true }),
    redactionFormat(),
    winston.format.json(),
  ),
  transports: [
    new winston.transports.Console({
      format:
        LOG_FORMAT === 'json'
          ? winston.format.combine(timestampFormat, redactionFormat(), winston.format.json())
          : winston.format.combine(
              timestampFormat,
              redactionFormat(),
              winston.format.colorize({ level: true }),
              prettyConsoleFormat,
            ),
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'app.log'),
      format: jsonFileFormat,
      maxsize: 10 * 1024 * 1024,
      maxFiles: 5,
      tailable: true,
      zippedArchive: false,
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'error.log'),
      level: 'warn',
      format: jsonFileFormat,
      maxsize: 5 * 1024 * 1024,
      maxFiles: 3,
      tailable: true,
      zippedArchive: false,
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'app-readable.log'),
      format: humanReadableFileFormat,
      maxsize: 10 * 1024 * 1024,
      maxFiles: 3,
      tailable: true,
      zippedArchive: false,
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'error-readable.log'),
      level: 'warn',
      format: humanReadableFileFormat,
      maxsize: 5 * 1024 * 1024,
      maxFiles: 2,
      tailable: true,
      zippedArchive: false,
    }),
  ],
});
let currentWriteAcked = true;
let currentWriteId: number | undefined;
let currentWriteTimeout: ReturnType<typeof setTimeout> | undefined;

function ackCurrentWrite(failed: boolean): void {
  if (currentWriteAcked) return;
  currentWriteAcked = true;
  if (currentWriteTimeout) {
    clearTimeout(currentWriteTimeout);
    currentWriteTimeout = undefined;
  }
  if (failed) {
    stats.writes_failed += 1;
    stats.last_write_failure_time = new Date().toISOString();
  } else {
    stats.last_write_time = new Date().toISOString();
  }
  parentPort?.postMessage({ type: 'written', id: currentWriteId, stats: { ...stats }, failed });
  currentWriteId = undefined;
}

logger.on('error', () => {
  ackCurrentWrite(true);
});

function flushTransports(): Promise<void> {
  return new Promise((resolve) => {
    const fileTransports = logger.transports.filter((t) => t instanceof winston.transports.File);
    let remaining = fileTransports.length;
    if (remaining === 0) {
      resolve();
      return;
    }
    for (const transport of fileTransports) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const stream = (transport as any)._dest || (transport as any)._stream;
      if (stream && typeof stream.fd === 'number') {
        try {
          fs.fdatasyncSync(stream.fd);
        } catch {
          // best-effort — fd may be closed during rotation
        }
      }
      remaining -= 1;
      if (remaining === 0) resolve();
    }
  });
}

parentPort?.on(
  'message',
  (event: LogMessage | FlushMessage | ShutdownMessage | QueueStatsMessage) => {
    if (event.type === 'log') {
      currentWriteAcked = false;
      currentWriteId = event.id;
      currentWriteTimeout = setTimeout(() => ackCurrentWrite(true), 5000);

      logger.write(
        {
          level: event.level,
          message: event.message,
          ...event.meta,
          timestamp: event.timestamp,
        },
        () => ackCurrentWrite(false),
      );
      return;
    }

    if (event.type === 'queue_stats') {
      logger.warn('logging.queue.stats', {
        accepted: event.accepted,
        droppedByLevel: event.droppedByLevel,
        queueHighWater: event.queueHighWater,
      });
      return;
    }

    if (event.type === 'flush') {
      const startedAt = Date.now();
      flushTransports()
        .then(() => {
          const duration = Date.now() - startedAt;
          stats.flush_count += 1;
          totalFlushDurationMs += duration;
          stats.avg_flush_duration_ms = totalFlushDurationMs / stats.flush_count;
          parentPort?.postMessage({ type: 'flushed', id: event.id, stats: { ...stats } });
        })
        .catch(() => {
          const duration = Date.now() - startedAt;
          stats.flush_count += 1;
          totalFlushDurationMs += duration;
          stats.avg_flush_duration_ms = totalFlushDurationMs / stats.flush_count;
          parentPort?.postMessage({ type: 'flushed', id: event.id, stats: { ...stats } });
        });
      return;
    }

    logger.once('finish', () => {
      parentPort?.postMessage({
        type: 'shutdown_complete',
        id: event.id,
        stats: { ...stats },
      });
      parentPort?.close();
    });
    logger.end();
  },
);
