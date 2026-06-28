import { TelegramHandlers } from '../../../../../src/services/telegram/handlers/telegram-handlers';

describe('TelegramHandlers', () => {
  it('registers /start, /help, /status, and /cancel commands', () => {
    const bot = {
      command: jest.fn(),
      on: jest.fn(),
    } as any;
    const commandHandlers = {
      handleStart: jest.fn(),
      handleHelp: jest.fn(),
      handleStatus: jest.fn(),
      handleCancel: jest.fn(),
    } as any;
    const messageHandlers = {
      handleText: jest.fn(),
      handleVoice: jest.fn(),
      handleAudio: jest.fn(),
      handlePhoto: jest.fn(),
      handleSticker: jest.fn(),
      handleVideoNote: jest.fn(),
      handleAnimation: jest.fn(),
      handleDocument: jest.fn(),
      handleUnknown: jest.fn(),
    } as any;
    const callbackHandler = {
      handleCallbackQuery: jest.fn(),
    } as any;
    const handlers = new TelegramHandlers(commandHandlers, messageHandlers, callbackHandler);

    handlers.setupHandlers(bot);

    expect(bot.command).toHaveBeenCalledTimes(4);
    expect(bot.command).toHaveBeenNthCalledWith(1, 'start', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(2, 'help', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(3, 'status', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(4, 'cancel', expect.any(Function));
  });
});
