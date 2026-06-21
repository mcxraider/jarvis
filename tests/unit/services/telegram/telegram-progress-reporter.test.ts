import { TelegramProgressReporter } from '../../../../src/services/telegram/telegram-progress-reporter';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';

const ROTATION_INTERVAL_MS = 10_000;

describe('TelegramProgressReporter', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    setRichMessagesEnabled(false);
  });

  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  function createContext() {
    return {
      chat: { id: 123 },
      reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      telegram: {
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
      },
    } as any;
  }

  it('rotates the plain status every 10 seconds and deletes it on completion', async () => {
    const ctx = createContext();
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

    await reporter.start();
    expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.\\.\\.', { parse_mode: 'MarkdownV2' });

    await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Fetching',
      { parse_mode: 'MarkdownV2' },
    );

    await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Writing',
      { parse_mode: 'MarkdownV2' },
    );

    await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Thinking\\.\\.\\.',
      { parse_mode: 'MarkdownV2' },
    );

    await reporter.complete('Done');
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);

    const editsAtCompletion = ctx.telegram.editMessageText.mock.calls.length;
    await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS * 2);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(editsAtCompletion);
  });

  it('keeps the agent progress callback UI-neutral', async () => {
    const ctx = createContext();
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

    await reporter.start();
    await reporter.record({
      sequence: 1,
      stage: 'tools_calling',
      message: 'Calling Todoist (1 request(s))',
    });

    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();
    await reporter.complete('Done');
  });

  it('continues safely when plain edits or deletion fail', async () => {
    const ctx = createContext();
    ctx.telegram.editMessageText.mockRejectedValue(new Error('edit failed'));
    ctx.telegram.deleteMessage.mockRejectedValue(new Error('delete failed'));
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

    await reporter.start();
    await expect(jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS)).resolves.toBeUndefined();
    await expect(reporter.complete('Something went wrong')).resolves.toBeUndefined();
  });

  describe('rich mode', () => {
    function createRichContext() {
      return {
        chat: { id: 123 },
        reply: jest.fn().mockResolvedValue({ message_id: 77 }),
        telegram: {
          editMessageText: jest.fn().mockResolvedValue(true),
          deleteMessage: jest.fn().mockResolvedValue(true),
          callApi: jest.fn().mockResolvedValue(true),
        },
      } as any;
    }

    it('rotates one ephemeral thinking draft without persisting a status message', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.start();
      await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);
      await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);
      await reporter.complete('Done');

      expect(ctx.reply).not.toHaveBeenCalled();
      expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();
      expect(ctx.telegram.deleteMessage).not.toHaveBeenCalled();

      const calls = ctx.telegram.callApi.mock.calls;
      expect(calls.map((call: unknown[]) => call[0])).toEqual([
        'sendRichMessageDraft',
        'sendRichMessageDraft',
        'sendRichMessageDraft',
      ]);
      expect(calls.map((call: any[]) => call[1].rich_message.markdown)).toEqual([
        '<tg-thinking><tg-emoji emoji-id="5573333417954639880">😀</tg-emoji> Thinking...</tg-thinking>',
        '<tg-thinking><tg-emoji emoji-id="5573333417954639880">😀</tg-emoji> Fetching</tg-thinking>',
        '<tg-thinking><tg-emoji emoji-id="5573333417954639880">😀</tg-emoji> Writing</tg-thinking>',
      ]);

      const draftIds = calls.map((call: any[]) => call[1].draft_id);
      expect(draftIds[0]).toBeGreaterThan(0);
      expect(new Set(draftIds).size).toBe(1);
    });

    it('falls back to a rotating plain status when the first draft fails', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      ctx.telegram.callApi.mockRejectedValueOnce(new Error('404 method not found'));
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.start();
      expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.\\.\\.', { parse_mode: 'MarkdownV2' });

      await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);
      expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
        123,
        77,
        undefined,
        'Fetching',
        { parse_mode: 'MarkdownV2' },
      );

      await reporter.complete('Done');
      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(1);
    });

    it('switches to plain rotation when a later rich draft update fails', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      ctx.telegram.callApi
        .mockResolvedValueOnce(true)
        .mockRejectedValueOnce(new Error('draft update failed'));
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.start();
      await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);

      expect(ctx.reply).toHaveBeenCalledWith('Fetching', { parse_mode: 'MarkdownV2' });

      await jest.advanceTimersByTimeAsync(ROTATION_INTERVAL_MS);
      expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
        123,
        77,
        undefined,
        'Writing',
        { parse_mode: 'MarkdownV2' },
      );

      await reporter.complete('Done');
      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
    });
  });
});
