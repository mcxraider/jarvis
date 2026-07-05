import { Message } from 'telegraf/typings/core/types/typegram';

const MAX_QUOTE_LEN = 700;

/**
 * Formats useful text from the Telegram message being replied to for inclusion
 * in a fresh agent request.
 */
export function formatReplyContext(
  replied: Message | undefined,
  botId: number | undefined,
): string | undefined {
  if (!replied) return undefined;

  const raw =
    ('text' in replied && replied.text) ||
    ('caption' in replied && replied.caption) ||
    undefined;
  if (!raw || !raw.trim()) return undefined;

  const quote =
    raw.length > MAX_QUOTE_LEN ? `${raw.slice(0, MAX_QUOTE_LEN)}…` : raw;
  const fromBot =
    replied.from?.is_bot === true ||
    (botId !== undefined && replied.from?.id === botId);
  const who = fromBot
    ? 'your earlier message'
    : `an earlier message from ${replied.from?.first_name ?? 'the user'}`;

  return `[In reply to ${who}: "${quote}"]`;
}
