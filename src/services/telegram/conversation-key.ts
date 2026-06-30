import crypto from 'crypto';

export function buildConversationKey(
  telegramUserId: number | undefined,
  internalUserId: string,
  chatId: number | string | undefined,
): string {
  if (chatId !== undefined) {
    const userSegment = telegramUserId ?? internalUserId;
    return `telegram-chat:${hashIdentifier(`${chatId}:${userSegment}`)}`;
  }
  return telegramUserId
    ? `telegram:${hashIdentifier(telegramUserId)}`
    : `internal:${internalUserId}`;
}

export function hashIdentifier(value: number | string): string {
  return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 32);
}

export function mapTelegramUserId(telegramUserId: number | undefined): string {
  if (!telegramUserId) return 'anonymous';
  const map = process.env.TELEGRAM_USER_MAP || '';
  const mappedUser = map
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => entry.split(':').map((value) => value.trim()))
    .find(([telegramId]) => telegramId === String(telegramUserId));
  return mappedUser?.[1] || `telegram:${telegramUserId}`;
}
