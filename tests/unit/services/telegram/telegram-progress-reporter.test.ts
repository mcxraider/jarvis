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

  it('logs each successfully delivered user-visible label at info level', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'request-1' });

    await reporter.start();
    expect(infoSpy).toHaveBeenLastCalledWith('telegram.progress.rendered', expect.objectContaining({
      requestId: 'request-1',
      label: 'Thinking…',
      transport: 'plain',
      phase: 'request',
      deliveredAtMs: expect.any(Number),
    }));

    infoSpy.mockClear();
    await reporter.record({
      sequence: 1, stage: 'progress', message: 'ignored', fact: {
        phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
      },
    });
    await jest.advanceTimersByTimeAsync(4_000);

    expect(infoSpy).toHaveBeenCalledTimes(1);
    expect(infoSpy).toHaveBeenLastCalledWith('telegram.progress.rendered', expect.objectContaining({
      requestId: 'request-1',
      label: 'Pulling up Calendar…',
      transport: 'plain',
      phase: 'lookup',
      sequence: 1,
      deliveredAtMs: expect.any(Number),
    }));
    await reporter.complete();
  });

  it('does not log a successful render when all delivery transports fail', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    ctx.telegram.callApi.mockRejectedValue(new Error('rich unavailable'));
    ctx.reply.mockRejectedValue(new Error('plain unavailable'));
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'request-2' });

    await reporter.start();

    expect(infoSpy).not.toHaveBeenCalledWith(
      'telegram.progress.rendered',
      expect.anything(),
    );
    await reporter.complete();
  });

  it('uses semantic facts and respects the four-second render floor', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();
    expect(ctx.reply).toHaveBeenCalledWith('Thinking…', { parse_mode: 'MarkdownV2' });

    await reporter.record({
      sequence: 1, stage: 'progress', message: 'ignored', fact: {
        phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
      },
    });
    expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();
    await jest.advanceTimersByTimeAsync(4_000);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123, 77, undefined, 'Pulling up Calendar…', { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete();
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 77);
  });

  it('keeps one rich draft continuously refreshed through a 130-second run', async () => {
    setRichMessagesEnabled(true);
    const callTimes: number[] = [];
    const ctx = context('private');
    ctx.telegram.callApi.mockImplementation(async () => {
      callTimes.push(Date.now());
      return true;
    });
    const reporter = new TelegramProgressReporter(ctx);

    await reporter.start();
    await jest.advanceTimersByTimeAsync(130_000);

    const calls = ctx.telegram.callApi.mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(8);
    expect(calls.every((call: unknown[]) => call[0] === 'sendRichMessageDraft')).toBe(true);
    expect(new Set(calls.map((call: any[]) => call[1].draft_id)).size).toBe(1);
    expect(callTimes.slice(1).every((time, index) => time - callTimes[index] <= 20_000)).toBe(true);
    expect(calls.map((call: any[]) => call[1].rich_message.markdown)).toEqual(
      expect.arrayContaining([
        expect.stringContaining('Thinking — taking a little longer…'),
        expect.stringContaining('Thinking — still working…'),
        expect.stringContaining('Thinking — taking longer than expected…'),
      ]),
    );
    await reporter.complete();
  });

  it('does not send unchanged keepalives for a persistent plain status', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await jest.advanceTimersByTimeAsync(44_999);
    expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();
    await jest.advanceTimersByTimeAsync(1);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(1);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123, 77, undefined, 'Thinking — taking a little longer…', { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete();
  });

  it('ingests later graph phases while a Telegram refresh is blocked', async () => {
    setRichMessagesEnabled(true);
    let resolveRefresh!: () => void;
    const blockedRefresh = new Promise<void>((resolve) => { resolveRefresh = resolve; });
    const ctx = context('private');
    ctx.telegram.callApi
      .mockResolvedValueOnce(true)
      .mockReturnValueOnce(blockedRefresh)
      .mockResolvedValue(true);
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await jest.advanceTimersByTimeAsync(20_000);
    await expect(reporter.record({
      sequence: 2, stage: 'progress', message: 'ignored', fact: {
        phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
      },
    })).resolves.toBeUndefined();
    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);

    await jest.advanceTimersByTimeAsync(4_000);
    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);
    resolveRefresh();
    await Promise.resolve();
    await Promise.resolve();
    await jest.advanceTimersByTimeAsync(4_000);

    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(3);
    expect(ctx.telegram.callApi.mock.calls[2][1].rich_message.markdown)
      .toContain('Pulling up Calendar…');
    await reporter.complete();
  });

  it('falls back to plain once and stops rich keepalives after a draft failure', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    ctx.telegram.callApi
      .mockResolvedValueOnce(true)
      .mockRejectedValueOnce(new Error('read ECONNRESET'));
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await jest.advanceTimersByTimeAsync(20_000);
    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.reply).toHaveBeenLastCalledWith('Thinking…', { parse_mode: 'MarkdownV2' });
    await jest.advanceTimersByTimeAsync(24_999);
    expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();
    await jest.advanceTimersByTimeAsync(1);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(1);
    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);
    await reporter.complete();
  });

  it('recreates a plain status when Telegram no longer has the original message', async () => {
    const ctx = context('group');
    ctx.reply
      .mockResolvedValueOnce({ message_id: 77 })
      .mockResolvedValueOnce({ message_id: 78 });
    ctx.telegram.editMessageText
      .mockRejectedValueOnce(new Error('400: Bad Request: message to edit not found'));
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await jest.advanceTimersByTimeAsync(45_000);
    expect(ctx.reply).toHaveBeenCalledTimes(2);
    expect(ctx.reply).toHaveBeenLastCalledWith(
      'Thinking — taking a little longer…', { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete();
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(123, 78);
  });

  it('treats an unchanged plain edit as delivered without creating another status', async () => {
    const ctx = context('group');
    ctx.telegram.editMessageText
      .mockRejectedValueOnce(new Error('400: Bad Request: message is not modified'));
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();

    await jest.advanceTimersByTimeAsync(45_000);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(1);
    expect(ctx.reply).toHaveBeenCalledTimes(1);
    await reporter.complete();
  });

  it('drains an in-flight plain render and emits nothing after completion', async () => {
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
    await jest.advanceTimersByTimeAsync(130_000);
    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('ignores an already-aborted progress delivery', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx);
    await reporter.start();
    const controller = new AbortController();
    controller.abort();

    await reporter.record({
      sequence: 1, stage: 'progress', message: 'ignored', fact: {
        phase: 'lookup', action: 'started', domains: ['calendar'], intent: 'read',
      },
    }, controller.signal);
    await jest.advanceTimersByTimeAsync(45_000);

    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(1);
    expect(ctx.telegram.editMessageText.mock.calls[0][3])
      .toBe('Thinking — taking a little longer…');
    await reporter.complete();
  });

  it.each([
    ['image', 'Analysing image…'],
    ['images', 'Analysing images…'],
    ['audio', 'Listening…'],
    ['forwarded', 'Reviewing forwarded messages…'],
  ] as const)(
    'renders the same %s label on first paint via both the rich and plain transports',
    async (inputKind, label) => {
      const plainCtx = context('group');
      const plainReporter = new TelegramProgressReporter(plainCtx, {}, inputKind);
      await plainReporter.start();
      expect(plainCtx.reply).toHaveBeenCalledWith(label, { parse_mode: 'MarkdownV2' });
      await plainReporter.complete();

      setRichMessagesEnabled(true);
      const richCtx = context('private');
      const richReporter = new TelegramProgressReporter(richCtx, {}, inputKind);
      await richReporter.start();
      expect(richCtx.reply).not.toHaveBeenCalled();
      expect(richCtx.telegram.callApi.mock.calls[0][1].rich_message.markdown).toContain(label);
      await richReporter.complete();
    },
  );

  it('defaults to the Thinking… seed label when no input kind is given', async () => {
    const ctx = context('group');
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'no-kind' });
    await reporter.start();
    expect(ctx.reply).toHaveBeenCalledWith('Thinking…', { parse_mode: 'MarkdownV2' });
    await reporter.complete();
  });

  it('replays a Listening→Thinking transition on beginAgentPhase without a new draft or timer', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    const reporter = new TelegramProgressReporter(ctx, {}, 'audio');

    await reporter.startTranscribing();
    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(1);
    expect(ctx.telegram.callApi.mock.calls[0][1].rich_message.markdown).toContain('Listening…');

    await jest.advanceTimersByTimeAsync(4_000);
    const timerCountBeforeTransition = jest.getTimerCount();

    await reporter.beginAgentPhase();
    expect(jest.getTimerCount()).toBe(timerCountBeforeTransition);
    await jest.advanceTimersByTimeAsync(0);

    expect(ctx.telegram.callApi).toHaveBeenCalledTimes(2);
    expect(ctx.telegram.callApi.mock.calls[1][1].rich_message.markdown).toContain('Thinking…');
    const draftIds = ctx.telegram.callApi.mock.calls.map((call: any[]) => call[1].draft_id);
    expect(new Set(draftIds).size).toBe(1);

    await reporter.complete();
  });

  it('renders Thinking… via the plain path when beginAgentPhase fires after a rich fallback', async () => {
    setRichMessagesEnabled(true);
    const ctx = context('private');
    ctx.telegram.callApi.mockRejectedValue(new Error('rich unavailable'));
    const reporter = new TelegramProgressReporter(ctx, {}, 'audio');

    await reporter.startTranscribing();
    expect(ctx.reply).toHaveBeenCalledWith('Listening…', { parse_mode: 'MarkdownV2' });

    await jest.advanceTimersByTimeAsync(4_000);
    await reporter.beginAgentPhase();
    await jest.advanceTimersByTimeAsync(0);

    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123, 77, undefined, 'Thinking…', { parse_mode: 'MarkdownV2' },
    );
    await reporter.complete();
  });
});
