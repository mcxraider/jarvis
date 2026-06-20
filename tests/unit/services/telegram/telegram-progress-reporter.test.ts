import { TelegramProgressReporter } from '../../../../src/services/telegram/telegram-progress-reporter';

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
});
