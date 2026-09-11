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

  describe('Markdown links', () => {
    it('preserves a basic link in MarkdownV2', () => {
      expect(toTelegramMarkdownV2('See [Example](https://example.com) for details.')).toBe(
        'See [Example](https://example.com) for details\\.',
      );
    });

    it('preserves multiple links', () => {
      expect(
        toTelegramMarkdownV2('[A](https://a.com) and [B](https://b.com)'),
      ).toBe('[A](https://a.com) and [B](https://b.com)');
    });

    it('escapes special chars in label but not in URL', () => {
      expect(
        toTelegramMarkdownV2('[hello *world*](https://example.com/path?a=1&b=2)'),
      ).toBe('[hello \\*world\\*](https://example.com/path?a=1&b=2)');
    });

    it('handles URLs with parentheses (Wikipedia)', () => {
      expect(
        toTelegramMarkdownV2('[Foo](https://en.wikipedia.org/wiki/Foo_(bar))'),
      ).toBe('[Foo](https://en.wikipedia.org/wiki/Foo_(bar))');
    });

    it('leaves links inside code spans literal', () => {
      expect(toTelegramMarkdownV2('`[not a link](url)`')).toBe('`[not a link](url)`');
    });

    it('escapes plain text around links normally', () => {
      expect(toTelegramMarkdownV2('Note: [link](https://x.com)!')).toBe(
        'Note: [link](https://x.com)\\!',
      );
    });
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
