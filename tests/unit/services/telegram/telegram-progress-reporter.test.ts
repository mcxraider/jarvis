import { TelegramProgressReporter } from '../../../../src/services/telegram/telegram-progress-reporter';
import { setRichMessagesEnabled } from '../../../../src/services/telegram/formatters/telegram-rich';

describe('TelegramProgressReporter', () => {
  function createContext() {
    return {
      chat: { id: 123 },
      reply: jest.fn().mockResolvedValue({ message_id: 77 }),
      telegram: {
        editMessageText: jest.fn().mockResolvedValue(true),
      },
    } as any;
  }

  it('sends one status message and edits it with progress updates', async () => {
    const ctx = createContext();
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' }, 0);

    await reporter.start();
    await reporter.record({
      sequence: 1,
      stage: 'thinking',
      message: 'Thinking through the request (turn 1/8)',
    });
    await reporter.record({
      sequence: 2,
      stage: 'tools_calling',
      message: 'Calling Todoist (1 request(s))',
    });
    await reporter.complete('Done');

    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.telegram.editMessageText).toHaveBeenCalledTimes(3);
    expect(ctx.telegram.editMessageText).toHaveBeenLastCalledWith(
      123,
      77,
      undefined,
      expect.stringContaining('Jarvis finished'),
      { parse_mode: 'MarkdownV2' },
    );
  });

  it('does not throw when status edits fail', async () => {
    const ctx = createContext();
    ctx.telegram.editMessageText.mockRejectedValue(new Error('edit failed'));
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' }, 0);

    await reporter.start();
    await expect(
      reporter.record({
        stage: 'thinking',
        message: 'Thinking through the request',
      }),
    ).resolves.toBeUndefined();
  });

  it('keeps the full progress history instead of trimming to recent lines', async () => {
    const ctx = createContext();
    const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' }, 0);

    await reporter.start();
    for (let index = 1; index <= 12; index += 1) {
      await reporter.record({
        sequence: index,
        stage: 'progress',
        message: `Progress event ${index}`,
      });
    }
    await reporter.complete('Done');

    const lastEditText = ctx.telegram.editMessageText.mock.calls.at(-1)?.[3];
    expect(lastEditText).toContain('2\\. Progress event 1');
    expect(lastEditText).toContain('13\\. Progress event 12');
    expect(lastEditText).toContain('14\\. Done');
  });

  describe('rich mode', () => {
    afterEach(() => setRichMessagesEnabled(false));

    function createRichContext() {
      return {
        chat: { id: 123 },
        reply: jest.fn().mockResolvedValue({ message_id: 77 }),
        telegram: {
          editMessageText: jest.fn().mockResolvedValue(true),
          callApi: jest.fn().mockResolvedValue({ message_id: 9 }),
        },
      } as any;
    }

    it('streams drafts and persists the final state via sendRichMessage', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' }, 0);

      await reporter.start();
      await reporter.record({ sequence: 1, stage: 'thinking', message: 'Thinking' });
      await reporter.complete('Done');

      expect(ctx.reply).not.toHaveBeenCalled();
      expect(ctx.telegram.editMessageText).not.toHaveBeenCalled();

      const methods = ctx.telegram.callApi.mock.calls.map((c: unknown[]) => c[0]);
      expect(methods).toEqual(['sendRichMessageDraft', 'sendRichMessageDraft', 'sendRichMessage']);

      // Drafts share one non-zero draft_id; final call persists with markdown content.
      const draftCalls = ctx.telegram.callApi.mock.calls.filter(
        (c: unknown[]) => c[0] === 'sendRichMessageDraft',
      );
      const draftIds = draftCalls.map((c: any[]) => c[1].draft_id);
      expect(draftIds[0]).toBeGreaterThan(0);
      expect(new Set(draftIds).size).toBe(1);

      const finalCall = ctx.telegram.callApi.mock.calls.at(-1) as any[];
      expect(finalCall[1].rich_message.markdown).toContain('Jarvis finished');
    });

    it('falls back to the plain status message when the first draft fails', async () => {
      setRichMessagesEnabled(true);
      const ctx = createRichContext();
      ctx.telegram.callApi.mockRejectedValueOnce(new Error('404 method not found'));
      const reporter = new TelegramProgressReporter(ctx, { requestId: 'tg_test' }, 0);

      await reporter.start();
      await reporter.complete('Done');

      // Reverted to the MarkdownV2 send+edit path; no further rich calls.
      expect(ctx.reply).toHaveBeenCalledTimes(1);
      expect(ctx.telegram.editMessageText).toHaveBeenCalled();
      expect(ctx.telegram.callApi).toHaveBeenCalledTimes(1);
    });
  });
});
