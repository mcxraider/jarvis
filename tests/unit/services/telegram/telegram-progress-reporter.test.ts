import { TelegramProgressReporter } from '../../../../src/services/telegram/telegram-progress-reporter';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';
import { logger } from '../../../../src/utils/logger';

const DOT_INTERVAL_MS = 800;

describe('TelegramProgressReporter', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    setRichMessagesEnabled(false);
  });

  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
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

  it('animates the ellipsis on a fixed Thinking base and deletes it on completion', async () => {
    const ctx = createContext();
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

    await reporter.start();
    expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.', { parse_mode: 'MarkdownV2' });

    await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Thinking\\.\\.',
      { parse_mode: 'MarkdownV2' },
    );

    await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Thinking\\.\\.\\.',
      { parse_mode: 'MarkdownV2' },
    );

    // Fourth frame wraps back to a single dot.
    await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Thinking\\.',
      { parse_mode: 'MarkdownV2' },
    );

    await reporter.complete('Done');
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);

    const editsAtCompletion = ctx.telegram.editMessageText.mock.calls.length;
    await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS * 2);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(editsAtCompletion);
  });

  it('keeps transcribing static until the agent phase begins', async () => {
    const ctx = createContext();
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

    await reporter.startTranscribing();
    expect(ctx.reply).toHaveBeenCalledWith('Transcribing\\.', {
      parse_mode: 'MarkdownV2',
    });

    // No dot timer runs during transcription, so nothing is edited.
    await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS * 3);
    expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();

    await reporter.beginAgentPhase();
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Thinking\\.',
      { parse_mode: 'MarkdownV2' },
    );

    await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      'Thinking\\.\\.',
      { parse_mode: 'MarkdownV2' },
    );

    // The base never rotates to the transcription phrase once thinking starts.
    expect(ctx.telegram.editMessageText.mock.calls.flat()).not.toContain(
      'Transcribing\\.',
    );

    await reporter.complete('Done');
  });

  it('ignores agent transitions after completion and repeated transitions', async () => {
    const ctx = createContext();
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

    await reporter.startTranscribing();
    await reporter.beginAgentPhase();
    await reporter.beginAgentPhase();
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(1);

    await reporter.complete('Done');
    await reporter.beginAgentPhase();
    await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(1);
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
    await expect(jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS)).resolves.toBeUndefined();
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

    it('animates one ephemeral thinking draft without persisting a status message', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.start();
      await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
      await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
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
        '<tg-thinking><tg-emoji emoji-id="5573333417954639880">😀</tg-emoji> Thinking.</tg-thinking>',
        '<tg-thinking><tg-emoji emoji-id="5573333417954639880">😀</tg-emoji> Thinking..</tg-thinking>',
        '<tg-thinking><tg-emoji emoji-id="5573333417954639880">😀</tg-emoji> Thinking...</tg-thinking>',
      ]);

      const draftIds = calls.map((call: any[]) => call[1].draft_id);
      expect(draftIds[0]).toBeGreaterThan(0);
      expect(new Set(draftIds).size).toBe(1);
    });

    it('reuses one rich draft for transcribing and agent animation', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.startTranscribing();
      await reporter.beginAgentPhase();
      await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
      await reporter.complete('Done');

      const calls = ctx.telegram.callApi.mock.calls;
      expect(calls.map((call: any[]) => call[1].rich_message.markdown)).toEqual([
        expect.stringContaining('Transcribing.'),
        expect.stringContaining('Thinking.'),
        expect.stringContaining('Thinking..'),
      ]);
      expect(new Set(calls.map((call: any[]) => call[1].draft_id)).size).toBe(1);
    });

    it('falls back to an animated plain status when the first draft fails', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      ctx.telegram.callApi.mockRejectedValueOnce(new Error('404 method not found'));
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.start();
      expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.', { parse_mode: 'MarkdownV2' });

      await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
      expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
        123,
        77,
        undefined,
        'Thinking\\.\\.',
        { parse_mode: 'MarkdownV2' },
      );

      await reporter.complete('Done');
      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(1);
    });

    it('switches to plain animation when a later rich draft update fails', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      ctx.telegram.callApi
        .mockResolvedValueOnce(true)
        .mockRejectedValueOnce(new Error('draft update failed'));
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.start();
      await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);

      expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.\\.', { parse_mode: 'MarkdownV2' });

      await jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS);
      expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
        123,
        77,
        undefined,
        'Thinking\\.\\.\\.',
        { parse_mode: 'MarkdownV2' },
      );

      await reporter.complete('Done');
      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
    });

    it('handles a non-Error rejection from a detached rich animation tick', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      const warn = jest.spyOn(logger, 'warn').mockImplementation();
      ctx.telegram.callApi
        .mockResolvedValueOnce(true)
        .mockRejectedValueOnce(undefined);
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' });

      await reporter.start();
      await expect(jest.advanceTimersByTimeAsync(DOT_INTERVAL_MS)).resolves.toBeUndefined();

      expect(warn).toHaveBeenCalledWith(
        'telegram.rich.fallback',
        expect.objectContaining({ stage: 'progress.update', error: 'undefined' }),
      );
      expect(ctx.reply).toHaveBeenCalledWith('Thinking\\.\\.', { parse_mode: 'MarkdownV2' });
      await reporter.complete('Done');
    });
  });
});
