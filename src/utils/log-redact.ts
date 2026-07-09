export const SENSITIVE_KEY_PATTERN =
  /(authorization|token|secret|api[_-]?key|password|bot[_-]?token|openai[_-]?api[_-]?key|todoist[_-]?api[_-]?key|telegram[_-]?secret)/i;
export const PRIVATE_ID_KEY_PATTERN = /^(chat_?id|user_?id|from_?id|telegram_?user_?id)$/i;

export function redactValue(value: unknown, key = ''): unknown {
  if (SENSITIVE_KEY_PATTERN.test(key)) return '[REDACTED]';
  if (PRIVATE_ID_KEY_PATTERN.test(key)) return '[REDACTED_ID]';

  if (typeof value === 'string') {
    if (/^Bearer\s+/i.test(value)) return 'Bearer [REDACTED]';
    if (value.includes('api.telegram.org/file/bot')) return '[REDACTED_TELEGRAM_FILE_URL]';
    return value;
  }

  if (Array.isArray(value)) return value.map((item) => redactValue(item, key));
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
