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
});
