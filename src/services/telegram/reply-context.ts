import { Message } from 'telegraf/typings/core/types/typegram';

const MAX_QUOTE_LEN = 700;

function extractRichMessageText(richMessage: unknown): string | undefined {
  if (typeof richMessage === 'string') return richMessage;
  if (!richMessage || typeof richMessage !== 'object') return undefined;

  const rm = richMessage as Record<string, unknown>;

  // Shape 1: { markdown: "..." } — what we send
  if (typeof rm.markdown === 'string') return rm.markdown;
  // Shape 2: { text: "..." }
  if (typeof rm.text === 'string') return rm.text;
  // Shape 3: { blocks: [...] } — what Telegram actually returns
  if (Array.isArray(rm.blocks)) return extractTextFromBlocks(rm.blocks);

  return undefined;
}

/**
 * Recursively extracts text content from Telegram rich_message blocks.
 * Block structure varies but commonly contains `text`, `content`, or nested
 * arrays of inline elements with a `text` field.
 */
function extractTextFromBlocks(blocks: unknown[]): string | undefined {
  const parts: string[] = [];

  for (const block of blocks) {
    if (!block || typeof block !== 'object') continue;
    const b = block as Record<string, unknown>;

    // Direct text field on block
    if (typeof b.text === 'string') {
      parts.push(b.text);
      continue;
    }
    // Content field (string)
    if (typeof b.content === 'string') {
      parts.push(b.content);
      continue;
    }
    // Content field (array of inline elements)
    if (Array.isArray(b.content)) {
      const inlineText = b.content
        .map((item: unknown) => {
          if (typeof item === 'string') return item;
          if (
            item &&
            typeof item === 'object' &&
            typeof (item as Record<string, unknown>).text === 'string'
          ) {
            return (item as Record<string, unknown>).text as string;
          }
          return '';
        })
        .join('');
      if (inlineText) parts.push(inlineText);
      continue;
    }
    // Data-wrapped text: { type: "...", data: { text: "..." } }
    if (b.data && typeof b.data === 'object' && typeof (b.data as any).text === 'string') {
      parts.push((b.data as any).text);
      continue;
    }
    // Children array (recursive): { type: "...", children: [...] }
    if (Array.isArray(b.children)) {
      const childText = extractTextFromBlocks(b.children);
      if (childText) parts.push(childText);
      continue;
    }
    // Nested blocks
    if (Array.isArray(b.blocks)) {
      const nested = extractTextFromBlocks(b.blocks);
      if (nested) parts.push(nested);
    }
  }

  const result = parts.join('\n').trim();
  return result || undefined;
}

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
    ('rich_message' in replied && extractRichMessageText((replied as any).rich_message)) ||
    ('poll' in replied &&
      (replied as any).poll?.question &&
      `[Poll: ${(replied as any).poll.question}]`) ||
    ('sticker' in replied &&
      (replied as any).sticker?.emoji &&
      `[Sticker: ${(replied as any).sticker.emoji}]`) ||
    ('contact' in replied &&
      (replied as any).contact?.first_name &&
      `[Contact: ${(replied as any).contact.first_name}]`) ||
    ('location' in replied && '[Shared location]') ||
    undefined;
  if (!raw || !raw.trim()) return undefined;

  const quote = raw.length > MAX_QUOTE_LEN ? `${raw.slice(0, MAX_QUOTE_LEN)}…` : raw;
  const fromBot =
    replied.from?.is_bot === true || (botId !== undefined && replied.from?.id === botId);
  const who = fromBot
    ? 'your earlier message'
    : `an earlier message from ${replied.from?.first_name ?? 'the user'}`;

  return `[In reply to ${who}: "${quote}"]`;
}
