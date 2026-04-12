import { TelegramHandlers } from '../../../../../src/services/telegram/handlers/telegram-handlers';

describe('TelegramHandlers', () => {
  it('registers only /help and /status commands', () => {
    const bot = {
      command: jest.fn(),
      on: jest.fn(),
    } as any;
    const fileService = {} as any;
    const messageProcessor = {} as any;
    const activityService = {
      recordActivity: jest.fn(),
    } as any;
    const statusService = {
      getFormattedStatus: jest.fn(),
    } as any;
    const handlers = new TelegramHandlers(
      fileService,
      messageProcessor,
      activityService,
      statusService,
    );

    handlers.setupHandlers(bot);

    expect(bot.command).toHaveBeenCalledTimes(2);
    expect(bot.command).toHaveBeenNthCalledWith(1, 'help', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(2, 'status', expect.any(Function));
    expect(bot.command).not.toHaveBeenCalledWith('start', expect.any(Function));
  });
});
