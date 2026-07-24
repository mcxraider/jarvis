import { TelegramNarrationReporter } from '../../../../src/services/telegram/telegram-narration-reporter';

describe('TelegramNarrationReporter', () => {
  let mockCtx: any;

  beforeEach(() => {
    mockCtx = {
      chat: { id: 123, type: 'private' },
      reply: jest.fn().mockResolvedValue({ message_id: 42, chat: { id: 123 }, date: 0, text: '' }),
      telegram: {
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      },
    };
  });

  it('sends a new italic message on first record()', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.record('Looking up your task...');
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
    const [text, opts] = mockCtx.reply.mock.calls[0];
    expect(text).toMatch(/^_.*_$/); // wrapped in italic markers
    expect(opts.parse_mode).toBe('MarkdownV2');
  });

  it('edits the existing message on subsequent record()', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.record('First');
    await reporter.record('Second');
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
    expect(mockCtx.telegram.editMessageText).toHaveBeenCalledTimes(1);
    expect(mockCtx.telegram.editMessageText).toHaveBeenCalledWith(
      123, 42, undefined,
      expect.stringMatching(/^_.*Second.*_$/),
      expect.objectContaining({ parse_mode: 'MarkdownV2' }),
    );
  });

  it('skips duplicate text', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.record('Same');
    await reporter.record('Same');
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
    expect(mockCtx.telegram.editMessageText).not.toHaveBeenCalled();
  });

  it('skips empty text', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.record('');
    await reporter.record('   ');
    expect(mockCtx.reply).not.toHaveBeenCalled();
  });

  it('deletes message on complete()', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.record('text');
    await reporter.complete();
    expect(mockCtx.telegram.deleteMessage).toHaveBeenCalledWith(123, 42);
  });

  it('complete() is safe without any record()', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.complete();
    expect(mockCtx.telegram.deleteMessage).not.toHaveBeenCalled();
  });

  it('is a no-op after complete()', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.complete();
    await reporter.record('ignored');
    expect(mockCtx.reply).not.toHaveBeenCalled();
  });

  it('handles reply error gracefully', async () => {
    mockCtx.reply.mockRejectedValue(new Error('Telegram API error'));
    const reporter = new TelegramNarrationReporter(mockCtx);
    // Should not throw
    await reporter.record('text');
    expect(mockCtx.reply).toHaveBeenCalledTimes(1);
  });

  it('handles delete error gracefully', async () => {
    mockCtx.telegram.deleteMessage.mockRejectedValue(new Error('message not found'));
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.record('text');
    // Should not throw
    await reporter.complete();
  });

  it('escapes MarkdownV2 special characters', async () => {
    const reporter = new TelegramNarrationReporter(mockCtx);
    await reporter.record('Hello! [test] (parens)');
    const [text] = mockCtx.reply.mock.calls[0];
    // All special chars should be escaped within the italic wrapper
    expect(text).toContain('\\!');
    expect(text).toContain('\\[');
    expect(text).toContain('\\(');
  });
});
