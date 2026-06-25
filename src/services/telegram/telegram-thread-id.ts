import { hashIdentifier } from './conversation-key';

export function buildTelegramThreadId(
  telegramUserId: number | undefined,
  internalUserId: string,
): string {
  const identity = telegramUserId ?? internalUserId;
  return `tg_user_${hashIdentifier(identity)}`;
}
