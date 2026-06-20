import {
  escapeMarkdownV2,
  inlineCode,
  replyWithMarkdown,
  toTelegramMarkdownV2,
} from '../../../../../src/services/telegram/formatters/telegram-markdown';

describe('telegram-markdown formatter', () => {
  it('escapes Telegram MarkdownV2 reserved characters in dynamic text', () => {
    expect(escapeMarkdownV2('task. due at 10am (p1)!')).toBe(
      'task\\. due at 10am \\(p1\\)\\!',
    );
  });

  it('converts common Markdown to Telegram MarkdownV2', () => {
    expect(toTelegramMarkdownV2('**Today**\n- Buy milk at 7.')).toBe(
      '*Today*\n\\- Buy milk at 7\\.',
    );
  });

  it('preserves inline code safely', () => {
    expect(toTelegramMarkdownV2(`File: ${inlineCode('voice_file.ogg')}`)).toBe(
      'File: `voice_file.ogg`',
    );
  });

  it('falls back to plain text if Telegram rejects MarkdownV2', async () => {
    const reply = jest
      .fn()
      .mockRejectedValueOnce(new Error('Bad Request: can\'t parse entities'))
      .mockResolvedValueOnce({});

    await replyWithMarkdown(reply as any, '**Done.**');

    expect(reply).toHaveBeenNthCalledWith(1, '*Done\\.*', { parse_mode: 'MarkdownV2' });
    expect(reply).toHaveBeenNthCalledWith(2, '**Done.**');
  });
});
