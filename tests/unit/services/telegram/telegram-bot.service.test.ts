jest.mock('telegraf', () => {
  const pTimeout = require('p-timeout');

  class MockTelegraf {
    public telegram = {
      setWebhook: jest.fn().mockResolvedValue(undefined),
      deleteWebhook: jest.fn().mockResolvedValue(undefined),
      sendMessage: jest.fn().mockResolvedValue({}),
      getMe: jest.fn().mockResolvedValue({}),
      setMyCommands: jest.fn().mockResolvedValue(undefined),
    };
    private middleware?: (ctx: any) => Promise<void> | void;
    private errorHandler?: (error: unknown, ctx: any) => Promise<void> | void;
    public command = jest.fn();
    public on = jest.fn((_event: string, handler: (ctx: any) => Promise<void> | void) => {
      this.middleware = handler;
    });
    public catch = jest.fn((handler: (error: unknown, ctx: any) => Promise<void> | void) => {
      this.errorHandler = handler;
    });
    public handleUpdate = jest.fn(async (update: any) => {
      if (!this.middleware) return;
      const chatId = update.message?.chat?.id ?? update.callback_query?.message?.chat?.id;
      const ctx = {
        update,
        from: update.message?.from ?? update.callback_query?.from,
        chat: chatId === undefined ? undefined : { id: chatId },
        reply: jest.fn((text: string) => this.telegram.sendMessage(chatId, text)),
      };
      try {
        await pTimeout(Promise.resolve(this.middleware(ctx)), this.options.handlerTimeout);
      } catch (error) {
        await this.errorHandler?.(error, ctx);
      }
    });
    public launch = jest.fn().mockResolvedValue(undefined);
    public stop = jest.fn();

    constructor(
      public readonly token: string,
      public readonly options: { handlerTimeout: number },
    ) {}
  }

  return {
    Telegraf: MockTelegraf,
  };
});

import { createTerminalReplyStore } from '../../../../src/services/telegram/terminal-reply.store';

describe('TelegramBotService', () => {
  async function createService() {
    const { TelegramBotService } = await import('../../../../src/services/telegram/telegram-bot.service');
    const handlers = {
      setupHandlers: jest.fn(),
    } as any;
    return new TelegramBotService(
      {
        token: 'bot-token',
        allowedUserIds: [701122767],
        webhookUrl: 'https://example.com',
        secretToken: 'secret',
      },
      handlers,
      createTerminalReplyStore(),
    );
  }

  afterEach(() => {
    jest.resetModules();
  });

  const EXPECTED_MENU_COMMANDS = [
    { command: 'new', description: 'Abandon the current step and start a new request' },
    { command: 'send_forward', description: 'Send buffered forwards to Jarvis with an instruction' },
    { command: 'cancel', description: 'Cancel the current operation' },
    { command: 'help', description: 'Show available commands and supported inputs' },
  ];

  it('syncs the full command menu before webhook setup', async () => {
    const service = await createService();

    await service.setupWebhook('https://example.com', 'secret');

    expect(service.bot.telegram.setMyCommands).toHaveBeenCalledWith(EXPECTED_MENU_COMMANDS);
    expect(service.bot.telegram.setWebhook).toHaveBeenCalledWith(
      'https://example.com/webhook/secret',
      expect.any(Object),
    );
  });

  it('uses the centralized 195 second handler watchdog by default', async () => {
    const originalHandlerTimeout = process.env.TELEGRAM_HANDLER_TIMEOUT_MS;
    delete process.env.TELEGRAM_HANDLER_TIMEOUT_MS;
    try {
      const service = await createService();

      expect((service.bot as any).options.handlerTimeout).toBe(195_000);
      await service.stop();
    } finally {
      if (originalHandlerTimeout === undefined) {
        delete process.env.TELEGRAM_HANDLER_TIMEOUT_MS;
      } else {
        process.env.TELEGRAM_HANDLER_TIMEOUT_MS = originalHandlerTimeout;
      }
    }
  });

  it('syncs the full command menu before polling starts', async () => {
    const service = await createService();

    await service.startPolling();

    expect(service.bot.telegram.setMyCommands).toHaveBeenCalledWith(EXPECTED_MENU_COMMANDS);
    expect(service.bot.launch).toHaveBeenCalled();
  });

  it('syncs commands registered during startup composition', async () => {
    const { TelegramBotService } = await import('../../../../src/services/telegram/telegram-bot.service');
    const { TelegramMenuRegistry } = await import('../../../../src/services/telegram/telegram-menu.registry');
    const menuRegistry = new TelegramMenuRegistry();
    menuRegistry.register({
      command: 'schedule_this_week',
      description: 'Show this week schedule',
    });
    const service = new TelegramBotService(
      { token: 'bot-token', allowedUserIds: [701122767] },
      { setupHandlers: jest.fn() } as any,
      createTerminalReplyStore(),
      undefined,
      menuRegistry,
    );

    await service.startPolling();

    expect(service.bot.telegram.setMyCommands).toHaveBeenCalledWith([
      ...EXPECTED_MENU_COMMANDS,
      { command: 'schedule_this_week', description: 'Show this week schedule' },
    ]);
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

  it('treats handler timeout as a watchdog and never sends a false generic failure', async () => {
    jest.useFakeTimers();
    try {
      const { TelegramBotService } = await import('../../../../src/services/telegram/telegram-bot.service');
      const { flushLogger, logger } = await import('../../../../src/utils/logger');
      const handlers = {
        setupHandlers: (bot: any) => {
          bot.on('text', async () => {
            await new Promise((resolve) => setTimeout(resolve, 200));
          });
        },
      } as any;
      const service = new TelegramBotService(
        {
          token: 'bot-token',
          allowedUserIds: [701122767],
          handlerTimeoutMs: 50,
        },
        handlers,
        createTerminalReplyStore(),
      );
      const update = {
        update_id: 1006,
        __requestId: 'tg_update_1006',
        message: {
          from: { id: 701122767 },
          chat: { id: 701122767 },
          text: 'Long request',
        },
      };

      const handling = service.handleUpdate(update);
      await jest.advanceTimersByTimeAsync(50);
      await handling;
      await flushLogger();

      expect(service.bot.telegram.sendMessage).not.toHaveBeenCalledWith(
        701122767,
        'Something went wrong. Please try again.',
      );
      expect(logger.error).toHaveBeenCalledWith(
        'telegram.handler.watchdog_expired',
        expect.objectContaining({
          requestId: 'tg_update_1006',
          durationMs: 50,
          handlerTimeoutMs: 50,
        }),
      );

      await jest.advanceTimersByTimeAsync(150);
      await service.stop();
    } finally {
      jest.useRealTimers();
    }
  });
});
