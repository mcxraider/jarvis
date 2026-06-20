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
});
