import winston from 'winston';
import fs from 'fs';
import path from 'path';

const LOG_DIR = path.join(process.cwd(), 'logs');
const LOG_LEVEL =
  process.env.LOG_LEVEL || (process.env.NODE_ENV === 'production' ? 'info' : 'debug');
const LOG_FORMAT =
  process.env.LOG_FORMAT || (process.env.NODE_ENV === 'production' ? 'json' : 'pretty');

const SENSITIVE_KEY_PATTERN =
  /(authorization|token|secret|api[_-]?key|password|bot[_-]?token|openai[_-]?api[_-]?key|todoist[_-]?api[_-]?key|telegram[_-]?secret)/i;
const PRIVATE_ID_KEY_PATTERN = /^(chat_?id|user_?id|from_?id|telegram_?user_?id)$/i;

function ensureLogDir(): void {
  fs.mkdirSync(LOG_DIR, { recursive: true });
}

function redactValue(value: unknown, key = ''): unknown {
  if (SENSITIVE_KEY_PATTERN.test(key)) {
    return '[REDACTED]';
  }

  if (PRIVATE_ID_KEY_PATTERN.test(key)) {
    return '[REDACTED_ID]';
  }

  if (typeof value === 'string') {
    if (/^Bearer\s+/i.test(value)) {
      return 'Bearer [REDACTED]';
    }

    if (value.includes('api.telegram.org/file/bot')) {
      return '[REDACTED_TELEGRAM_FILE_URL]';
    }

    return value;
  }

  if (Array.isArray(value)) {
    return value.map((item) => redactValue(item, key));
  }

  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([entryKey, entryValue]) => [
        entryKey,
        redactValue(entryValue, entryKey),
      ]),
    );
  }

  return value;
}

const redactionFormat = winston.format((info) => {
  Object.entries(info).forEach(([key, value]) => {
    info[key] = redactValue(value, key);
  });
  return info;
});

const READABLE_CONTEXT_KEYS = [
  'requestId',
  'updateId',
  'userId',
  'chatId',
  'messageId',
  'messageType',
  'threadId',
];

function formatReadableValue(value: unknown): string {
  if (value === undefined) {
    return 'undefined';
  }

  if (value === null || typeof value !== 'object') {
    return String(value);
  }

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
      ? [`  details:`, indentMultiline(JSON.stringify(remainingMetadata, null, 2))]
      : [];
  const stackLines = typeof stack === 'string' ? [`  stack:`, indentMultiline(stack)] : [];

  return [
    `[${timestamp}] ${level.toUpperCase()} ${message}`,
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

const jsonFileFormat = winston.format.combine(
  winston.format.timestamp(),
  redactionFormat(),
  winston.format.json(),
);

const humanReadableFileFormat = winston.format.combine(
  winston.format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss' }),
  redactionFormat(),
  readableFileFormat,
);

ensureLogDir();

export interface LogContext {
  requestId?: string;
  updateId?: number;
  userId?: number | string;
  chatId?: number | string;
  messageId?: number;
  messageType?: string;
  threadId?: string;
}

export function createRequestId(prefix = 'req'): string {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export function truncateForLog(value: string | undefined, maxLength = 80): string | undefined {
  if (!value) return value;
  return value.length > maxLength ? `${value.slice(0, maxLength)}...` : value;
}

export const logger = winston.createLogger({
  level: LOG_LEVEL,
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.errors({ stack: true }), // logs stack trace
    redactionFormat(),
    winston.format.json(),
  ),
  transports: [
    new winston.transports.Console({
      format:
        LOG_FORMAT === 'json'
          ? winston.format.combine(
              winston.format.timestamp(),
              redactionFormat(),
              winston.format.json(),
            )
          : winston.format.combine(
              winston.format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss' }),
              redactionFormat(),
              winston.format.colorize({ level: true }),
              prettyConsoleFormat,
            ),
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'app.log'),
      format: jsonFileFormat,
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'error.log'),
      level: 'warn',
      format: jsonFileFormat,
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'app-readable.log'),
      format: humanReadableFileFormat,
    }),
    new winston.transports.File({
      filename: path.join(LOG_DIR, 'error-readable.log'),
      level: 'warn',
      format: humanReadableFileFormat,
    }),
  ],
});
