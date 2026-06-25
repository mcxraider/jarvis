import { TelegramHandlers } from '../../../../../src/services/telegram/handlers/telegram-handlers';

describe('TelegramHandlers', () => {
  it('registers /help, /status, and /cancel commands', () => {
    const bot = {
      command: jest.fn(),
      on: jest.fn(),
    } as any;
    const commandHandlers = {
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

    expect(bot.command).toHaveBeenCalledTimes(3);
    expect(bot.command).toHaveBeenNthCalledWith(1, 'help', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(2, 'status', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(3, 'cancel', expect.any(Function));
    expect(bot.command).not.toHaveBeenCalledWith('start', expect.any(Function));
  });
});
