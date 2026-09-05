import { TelegramProgressReporter } from '../../../../src/services/telegram/telegram-progress-reporter';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';
import { logger } from '../../../../src/utils/logger';

describe('TelegramProgressReporter', () => {
  let infoSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.useFakeTimers();
    infoSpy = jest.spyOn(logger, 'info').mockImplementation(() => logger);
  });

  afterEach(() => {
    infoSpy.mockRestore();
    setRichMessagesEnabled(false);
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  function context(type: 'private' | 'group' = 'private') {
    return {
      chat: { id: 123, type },
      reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      telegram: {
        editMessageText: jest.fn().mockResolvedValue(true),
        deleteMessage: jest.fn().mockResolvedValue(true),
        callApi: jest.fn().mockResolvedValue(true),
      },
    } as any;
  }

  async function recordSummary(
    reporter: TelegramProgressReporter,
    text: string,
    sequence = 1,
    signal?: AbortSignal,
  ) {
    await reporter.record({
      sequence,
      stage: 'reasoning_summary',
      message: text,
      reasoningSummary: text,
    }, signal);
    await jest.advanceTimersByTimeAsync(0);
  }

  it.each([
    ['text', 'Thinking…'],
    ['image', 'Analysing image…'],
    ['images', 'Analysing images…'],
    ['audio', 'Listening…'],
    ['forwarded', 'Reviewing forwarded messages…'],
  ] as const)('renders the %s seed through rich and plain transports', async (kind, label) => {
    const plainCtx = context('group');
    const plainReporter = new TelegramProgressReporter(plainCtx, {}, kind);
    await plainReporter.start();
    expect(plainCtx.reply).toHaveBeenCalledWith(label, { parse_mode: 'MarkdownV2' });
    await plainReporter.complete();

    setRichMessagesEnabled(true);
    const richCtx = context('private');
    const richReporter = new TelegramProgressReporter(richCtx, {}, kind);
    await richReporter.start();
    expect(richCtx.reply).not.toHaveBeenCalled();
    expect(richCtx.telegram.callApi.mock.calls[0][1].rich_message.markdown).toContain(label);
    await richReporter.complete();
  });

  it('ignores semantic progress and never adds elapsed labels', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await reporter.record({
      sequence: 1,
      stage: 'progress',
      message: 'ignored',
      fact: { phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read' },
    });
    await jest.advanceTimersByTimeAsync(130_000);

    expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();
    expect(ctx.reply).toHaveBeenCalledTimes(1);
    await reporter.complete();
  });

  it('immediately replaces one rich draft with each reasoning summary', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await recordSummary(reporter, 'Checking your calendar.', 2);
    await recordSummary(reporter, 'Comparing the available times.', 3);

    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(3);
    const calls = ctx.telegram.callApi.mock.calls;
    expect(new Set(calls.map((call: any[]) => call[1].draft_id)).size).toBe(1);
    expect(calls[1][1].rich_message.markdown).toContain('Checking your calendar.');
    expect(calls[2][1].rich_message.markdown).toContain('Comparing the available times.');
    expect(ctx.reply).not.toHaveBeenCalled();
    await reporter.complete();
  });

  it('edits the initial plain message instead of creating a summary message', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await recordSummary(reporter, 'Looking through your tasks.');

    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledWith(
      123,
      77,
      undefined,
      'Looking through your tasks\\.',
      { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete();
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
  });

  it('keeps the current rich content alive every 20 seconds without changing it', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();
    await recordSummary(reporter, 'Still checking the same request.');

    await jest.advanceTimersByTimeAsync(60_000);

    const calls = ctx.telegram.callApi.mock.calls;
    expect(calls).toHaveLength(5);
    expect(new Set(calls.map((call: any[]) => call[1].draft_id)).size).toBe(1);
    expect(calls.slice(1).every((call: any[]) =>
      call[1].rich_message.markdown.includes('Still checking the same request.'),
    )).toBe(true);
    await reporter.complete();
  });

  it('refreshes Listening after transcription without switching to Thinking', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    const reporter = new TelegramProgressReporter(ctx, {}, 'audio');
    await reporter.start();

    await reporter.refresh();

    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);
    const calls = ctx.telegram.callApi.mock.calls;
    expect(calls[0][1].draft_id).toBe(calls[1][1].draft_id);
    expect(calls.every((call: any[]) => call[1].rich_message.markdown.includes('Listening…')))
      .toBe(true);
    expect(calls.some((call: any[]) => call[1].rich_message.markdown.includes('Thinking…')))
      .toBe(false);
    await reporter.complete();
  });

  it('coalesces updates arriving during an in-flight paint to the newest summary', async () => {
    setRichMessagesEnabled(true);
    let resolvePaint!: () => void;
    const blockedPaint = new Promise<void>((resolve) => { resolvePaint = resolve; });
    const ctx = context('private');
    ctx.telegram.callApi
      .mockResolvedValueOnce(true)
      .mockReturnValueOnce(blockedPaint)
      .mockResolvedValue(true);
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await reporter.record({ sequence: 1, stage: 'reasoning_summary', message: 'first', reasoningSummary: 'first' });
    await jest.advanceTimersByTimeAsync(0);
    await reporter.record({ sequence: 2, stage: 'reasoning_summary', message: 'second', reasoningSummary: 'second' });
    await reporter.record({ sequence: 3, stage: 'reasoning_summary', message: 'third', reasoningSummary: 'third' });
    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);

    resolvePaint();
    await Promise.resolve();
    await Promise.resolve();
    await jest.advanceTimersByTimeAsync(0);

    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(3);
    expect(ctx.telegram.callApi.mock.calls[2][1].rich_message.markdown).toContain('third');
    expect(ctx.telegram.callApi.mock.calls[2][1].rich_message.markdown).not.toContain('second');
    await reporter.complete();
  });

  it('ignores blank, duplicate, aborted, and post-completion summaries', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();
    await recordSummary(reporter, 'visible');

    await recordSummary(reporter, 'visible', 2);
    await recordSummary(reporter, '   ', 3);
    const controller = new AbortController();
    controller.abort();
    await recordSummary(reporter, 'aborted', 4, controller.signal);
    await reporter.complete();
    await recordSummary(reporter, 'late', 5);

    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(1);
  });

  it('clips long summaries and escapes model text in rich markup', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();
    const secret = 'a'.repeat(5_000) + ' <calendar> &';

    await recordSummary(reporter, secret);

    const markdown = ctx.telegram.callApi.mock.calls[1][1].rich_message.markdown as string;
    expect(markdown).not.toContain('<calendar>');
    expect(markdown).toContain('&lt;calendar&gt; &amp;');
    expect(markdown.length).toBeLessThan(4_100);
    expect(JSON.stringify(infoSpy.mock.calls)).not.toContain(secret);
    expect(infoSpy).toHaveBeenLastCalledWith('telegram.progress.rendered', expect.objectContaining({
      contentKind: 'reasoning_summary',
      sequence: 1,
      textLength: 3_800,
    }));
    await reporter.complete();
  });

  it('falls back from a failed rich update to one plain status', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    ctx.telegram.callApi
      .mockResolvedValueOnce(true)
      .mockRejectedValueOnce(new Error('rich unavailable'));
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await recordSummary(reporter, 'Use the fallback.');

    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.reply).toHaveBeenCalledWith('Use the fallback\\.', { parse_mode: 'MarkdownV2' });
    await reporter.complete();
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
  });

  it('recreates a missing plain status with the current summary', async () => {
    const ctx = context('group');
    ctx.reply
      .mockResolvedValueOnce({ message_id: 77 })
      .mockResolvedValueOnce({ message_id: 78 });
    ctx.telegram.editMessageText.mockRejectedValueOnce(
      new Error('400: Bad Request: message to edit not found'),
    );
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await recordSummary(reporter, 'Replacement status.');

    expect(ctx.reply).toHaveBeenCalledTimes(2);
    expect(ctx.reply).toHaveBeenLastCalledWith(
      'Replacement status\\.',
      { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete();
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 78);
  });

  it('retries a failed plain edit after five seconds', async () => {
    const ctx = context('group');
    ctx.telegram.editMessageText
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce(true);
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await recordSummary(reporter, 'Retry this summary.');
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(2);
    await jest.advanceTimersByTimeAsync(4_999);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(2);
    await jest.advanceTimersByTimeAsync(1);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(3);
    await reporter.complete();
  });

  it('drains an in-flight plain start and deletes the late message on completion', async () => {
    let resolveReply!: (message: { message_id: number }) => void;
    const reply = new Promise<{ message_id: number }>((resolve) => { resolveReply = resolve; });
    const ctx = context('group');
    ctx.reply.mockReturnValue(reply);
    const reporter = new TelegramProgressReporter(ctx);

    const start = reporter.start();
    await Promise.resolve();
    const completion = reporter.complete();
    resolveReply({ message_id: 88 });
    await Promise.all([start, completion]);

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 88);
    await jest.advanceTimersByTimeAsync(60_000);
    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });
});
