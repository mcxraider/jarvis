import {
  buildTelegramTextUpdate,
  parseSimulatorArgs,
  simulateTelegramUpdate,
} from '../../../scripts/simulate_telegram_update';

describe('Telegram webhook simulator', () => {
  it('selects an aliased user and builds a Telegram-shaped text update', () => {
    const options = parseSimulatorArgs(
      ['--user-2', '--username', '@tester', '--update-id', '9001', 'hello', 'Jarvis'],
      {
        JARVIS_CLI_USER_2_TELEGRAM_ID: '387244560',
        TELEGRAM_SECRET_TOKEN: 'secret',
        PORT: '3456',
      },
    );

    expect(options).toMatchObject({
      text: 'hello Jarvis',
      userId: 387244560,
      chatId: 387244560,
      username: 'tester',
      baseUrl: 'http://127.0.0.1:3456',
      updateId: 9001,
      messageId: 9001,
    });
    expect(buildTelegramTextUpdate(options)).toMatchObject({
      update_id: 9001,
      message: {
        message_id: 9001,
        chat: { id: 387244560, type: 'private' },
        from: { id: 387244560, is_bot: false, username: 'tester' },
        text: 'hello Jarvis',
      },
    });
  });

  it.each([
    ['--user-1', 'JARVIS_CLI_USER_1_TELEGRAM_ID', '701122767', 701122767],
    ['--user-2', 'JARVIS_CLI_USER_2_TELEGRAM_ID', '387244560', 387244560],
  ])('maps %s to its configured Telegram identity', (flag, envName, value, expected) => {
    const options = parseSimulatorArgs([flag, 'hello'], {
      [envName]: value,
      TELEGRAM_SECRET_TOKEN: 'secret',
    });

    expect(options.userId).toBe(expected);
    expect(options.chatId).toBe(expected);
  });

  it('supports an explicit user and a separate group chat id', () => {
    const options = parseSimulatorArgs(
      ['--telegram-user-id', '123', '--chat-id', '456', 'test group routing'],
      { TELEGRAM_SECRET_TOKEN: 'secret' },
    );
    expect(options.userId).toBe(123);
    expect(options.chatId).toBe(456);
  });

  it('requires exactly one resolvable identity and a message', () => {
    expect(() => parseSimulatorArgs(['hello'], { TELEGRAM_SECRET_TOKEN: 'secret' })).toThrow(
      'Choose --telegram-user-id',
    );
    expect(() =>
      parseSimulatorArgs(['--user-1', '--telegram-user-id', '123', 'hello'], {
        JARVIS_CLI_USER_1_TELEGRAM_ID: '701122767',
        TELEGRAM_SECRET_TOKEN: 'secret',
      }),
    ).toThrow('Choose only one');
  });

  it('posts to the configured webhook', async () => {
    const options = parseSimulatorArgs(['--telegram-user-id', '123', 'hello'], {
      TELEGRAM_SECRET_TOKEN: 'a/b',
    });
    const fetcher = jest.fn().mockResolvedValue({ ok: true, status: 200 });
    const log = jest.spyOn(console, 'log').mockImplementation();

    await simulateTelegramUpdate(options, fetcher as typeof fetch);

    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:3000/webhook/a%2Fb',
      expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('"text":"hello"'),
      }),
    );
    log.mockRestore();
  });
});
