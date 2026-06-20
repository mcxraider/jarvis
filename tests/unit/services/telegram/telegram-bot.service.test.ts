jest.mock('telegraf', () => {
  class MockTelegraf {
    public telegram = {
      setWebhook: jest.fn().mockResolvedValue(undefined),
      deleteWebhook: jest.fn().mockResolvedValue(undefined),
      sendMessage: jest.fn().mockResolvedValue({}),
      getMe: jest.fn().mockResolvedValue({}),
      setMyCommands: jest.fn().mockResolvedValue(undefined),
    };
    public command = jest.fn();
    public on = jest.fn();
    public catch = jest.fn();
    public handleUpdate = jest.fn().mockResolvedValue(undefined);
    public launch = jest.fn().mockResolvedValue(undefined);
    public stop = jest.fn();

    constructor(public readonly token: string) {}
  }

  return {
    Telegraf: MockTelegraf,
  };
});

describe('TelegramBotService', () => {
  const originalTodoistApiKey = process.env.TODOIST_API_KEY;
  const originalOpenAiModel = process.env.OPENAI_MODEL;

  beforeEach(() => {
    process.env.TODOIST_API_KEY = 'todoist-key';
    process.env.OPENAI_MODEL = 'gpt-4o';
  });

  afterEach(() => {
    jest.resetModules();

    if (originalTodoistApiKey === undefined) {
      delete process.env.TODOIST_API_KEY;
    } else {
      process.env.TODOIST_API_KEY = originalTodoistApiKey;
    }

    if (originalOpenAiModel === undefined) {
      delete process.env.OPENAI_MODEL;
    } else {
      process.env.OPENAI_MODEL = originalOpenAiModel;
    }
  });

  async function createService() {
    const { TelegramBotService } = await import('../../../../src/services/telegram/telegram-bot.service');
    return new TelegramBotService(
      {
        token: 'bot-token',
        allowedUserIds: [701122767],
        webhookUrl: 'https://example.com',
        secretToken: 'secret',
      },
      {} as any,
    );
  }

  it('syncs only /help and /status before webhook setup', async () => {
    const service = await createService();

    await service.setupWebhook('https://example.com', 'secret');

    expect(service.bot.telegram.setMyCommands).toHaveBeenCalledWith([
      { command: 'help', description: 'Show available commands and supported inputs' },
      { command: 'status', description: 'Show bot health, uptime, and dependency status' },
    ]);
    expect(service.bot.telegram.setWebhook).toHaveBeenCalledWith(
      'https://example.com/webhook/secret',
      expect.any(Object),
    );
  });

  it('syncs only /help and /status before polling starts', async () => {
    const service = await createService();

    await service.startPolling();

    expect(service.bot.telegram.setMyCommands).toHaveBeenCalledWith([
      { command: 'help', description: 'Show available commands and supported inputs' },
      { command: 'status', description: 'Show bot health, uptime, and dependency status' },
    ]);
    expect(service.bot.launch).toHaveBeenCalled();
  });

  it('handles updates from allowed Telegram users', async () => {
    const service = await createService();
    const update = {
      update_id: 1001,
      message: {
        from: { id: 701122767 },
        chat: { id: 701122767 },
        text: 'Show my tasks',
      },
    };

    await service.handleUpdate(update);

    expect(service.bot.handleUpdate).toHaveBeenCalledWith(update);
    expect(service.bot.telegram.sendMessage).not.toHaveBeenCalled();
  });

  it('denies updates from unapproved Telegram users', async () => {
    const service = await createService();
    const update = {
      update_id: 1002,
      message: {
        from: { id: 123456 },
        chat: { id: 123456 },
        text: 'Show my tasks',
      },
    };

    await service.handleUpdate(update);

    expect(service.bot.handleUpdate).not.toHaveBeenCalled();
    expect(service.bot.telegram.sendMessage).toHaveBeenCalledWith(
      123456,
      'Sorry, this bot is private\\.',
      { parse_mode: 'MarkdownV2' },
    );
  });

  it('denies updates with no sender id', async () => {
    const service = await createService();

    await service.handleUpdate({ update_id: 1003, message: { chat: { id: 123456 } } });

    expect(service.bot.handleUpdate).not.toHaveBeenCalled();
    expect(service.bot.telegram.sendMessage).toHaveBeenCalledWith(
      123456,
      'Sorry, this bot is private\\.',
      { parse_mode: 'MarkdownV2' },
    );
  });

  it('does not throw when denying an update without a usable chat id', async () => {
    const service = await createService();

    await expect(service.handleUpdate({ update_id: 1004 })).resolves.toBeUndefined();

    expect(service.bot.handleUpdate).not.toHaveBeenCalled();
    expect(service.bot.telegram.sendMessage).not.toHaveBeenCalled();
  });

  it('checks callback query sender ids before handling updates', async () => {
    const service = await createService();
    const update = {
      update_id: 1005,
      callback_query: {
        from: { id: 701122767 },
        message: { chat: { id: 701122767 } },
      },
    };

    await service.handleUpdate(update);

    expect(service.bot.handleUpdate).toHaveBeenCalledWith(update);
  });
});
