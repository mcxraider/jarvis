import { Message } from 'telegraf/typings/core/types/typegram';

const MAX_QUOTE_LEN = 700;

function extractInline(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  if (Array.isArray(value)) return value.map(extractInline).join('');
  if (typeof value === 'object') {
    const o = value as Record<string, unknown>;
    if ('text' in o) return extractInline(o.text);
    if (typeof o.expression === 'string') return o.expression;
    return '';
  }
  return '';
}

function extractRichMessageText(richMessage: unknown): string | undefined {
  if (typeof richMessage === 'string') return richMessage;
  if (!richMessage || typeof richMessage !== 'object') return undefined;

  const rm = richMessage as Record<string, unknown>;

  if (typeof rm.markdown === 'string') return rm.markdown;
  if (typeof rm.text === 'string') return rm.text;
  if (Array.isArray(rm.blocks)) return extractTextFromBlocks(rm.blocks);

  return undefined;
}

function extractTextFromBlocks(blocks: unknown[]): string | undefined {
  const parts: string[] = [];

  for (const block of blocks) {
    if (!block || typeof block !== 'object') continue;
    const b = block as Record<string, unknown>;

    if (b.type === 'divider') continue;

    if (b.type === 'list' && Array.isArray(b.items)) {
      for (const item of b.items as Record<string, unknown>[]) {
        if (!item || typeof item !== 'object') continue;
        let prefix = typeof item.label === 'string' ? `${item.label} ` : '';
        if (item.has_checkbox) prefix = (item.is_checked ? '☑ ' : '☐ ') + prefix;
        const itemText = Array.isArray(item.blocks)
          ? extractTextFromBlocks(item.blocks) ?? ''
          : '';
        parts.push(prefix + itemText);
      }
      continue;
    }

    if (b.type === 'details') {
      const summary = extractInline(b.summary);
      if (summary) parts.push(summary);
      if (Array.isArray(b.blocks)) {
        const nested = extractTextFromBlocks(b.blocks);
        if (nested) parts.push(nested);
      }
      continue;
    }

    if (b.type === 'table' && Array.isArray(b.cells)) {
      for (const row of b.cells as unknown[][]) {
        if (!Array.isArray(row)) continue;
        const cells = row.map((cell: unknown) => {
          if (!cell || typeof cell !== 'object') return '';
          return extractInline((cell as Record<string, unknown>).text);
        });
        const rowText = cells.join(' | ').trim();
        if (rowText) parts.push(rowText);
      }
      continue;
    }

    // Generic fallbacks (also covers blockquote via b.blocks, footer/heading/paragraph via b.text)
    if ('text' in b) {
      const t = extractInline(b.text);
      if (t) { parts.push(t); continue; }
    }
    if (typeof b.content === 'string') {
      parts.push(b.content);
      continue;
    }
    if (Array.isArray(b.content)) {
      const t = extractInline(b.content);
      if (t) { parts.push(t); continue; }
    }
    if (b.data && typeof b.data === 'object' && typeof (b.data as any).text === 'string') {
      parts.push((b.data as any).text);
      continue;
    }
    if (Array.isArray(b.children)) {
      const childText = extractTextFromBlocks(b.children);
      if (childText) parts.push(childText);
      continue;
    }
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
    ('rich_message' in replied && extractRichMessageText((replied as any).rich_message)) ||
    ('text' in replied && replied.text) ||
    ('caption' in replied && replied.caption) ||
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
