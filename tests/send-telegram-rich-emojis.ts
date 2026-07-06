import 'dotenv/config';
import { renderThinkingLabelWithEmoji } from '../src/services/telegram/formatters/telegram-rich';

const botToken = process.env.BOT_TOKEN;
const chatId = process.env.TEST_CHAT_ID;
const stickerSetName = process.env.TEST_TELEGRAM_EMOJI_SET || 'AIActions';
const PREVIEW_INTERVAL_MS = 4_000;

if (!botToken || !chatId) {
  throw new Error('BOT_TOKEN and TEST_CHAT_ID must be set (for example, in .env)');
}

interface TelegramResponse<T> {
  ok: boolean;
  result?: T;
  description?: string;
}

interface TelegramSticker {
  emoji?: string;
  custom_emoji_id?: string;
}

interface TelegramStickerSet {
  name: string;
  title: string;
  stickers: TelegramSticker[];
}

async function callTelegram<T>(
  method: string,
  payload: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(`https://api.telegram.org/bot${botToken}/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const body = (await response.json()) as TelegramResponse<T>;

  if (!response.ok || !body.ok || body.result === undefined) {
    throw new Error(`${method} failed: ${body.description ?? response.statusText}`);
  }

  return body.result;
}

function customEmojis(set: TelegramStickerSet) {
  return set.stickers.filter(
    (sticker): sticker is TelegramSticker & { custom_emoji_id: string } =>
      Boolean(sticker.custom_emoji_id),
  );
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function main(): Promise<void> {
  const set = await callTelegram<TelegramStickerSet>('getStickerSet', {
    name: stickerSetName,
  });
  const emojis = customEmojis(set);

  if (emojis.length === 0) {
    throw new Error(`Sticker set ${set.name} contains no custom emojis`);
  }

  const draftId = Date.now() & 0x7fffffff || 1;

  for (const [index, sticker] of emojis.entries()) {
    const fallback = sticker.emoji || '🙂';
    const label = `${index}: ${sticker.custom_emoji_id}`;
    const markdown = renderThinkingLabelWithEmoji(
      label,
      sticker.custom_emoji_id,
      fallback,
    );

    await callTelegram('sendRichMessageDraft', {
      chat_id: chatId,
      draft_id: draftId,
      rich_message: { markdown },
    });
    console.log(`Previewed emoji ${index + 1}/${emojis.length}: ${sticker.custom_emoji_id}`);

    if (index < emojis.length - 1) {
      await sleep(PREVIEW_INTERVAL_MS);
    }
  }

  await callTelegram('sendRichMessage', {
    chat_id: chatId,
    rich_message: {
      markdown: `**${set.title} preview complete**\n\nPreviewed ${emojis.length} custom emojis.`,
    },
  });
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
