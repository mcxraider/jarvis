import { TelegramHandlers } from '../../../../../src/services/telegram/handlers/telegram-handlers';

describe('TelegramHandlers', () => {
  it('registers /start, /help, /status, /cancel, /new, and /forward commands', () => {
    const bot = {
      command: jest.fn(),
      on: jest.fn(),
      use: jest.fn(),
    } as any;
    const commandHandlers = {
      handleStart: jest.fn(),
      handleHelp: jest.fn(),
      handleStatus: jest.fn(),
      handleCancel: jest.fn(),
    } as any;
    const messageHandlers = {
      handleText: jest.fn(),
      handleNew: jest.fn(),
      handleForward: jest.fn(),
      maybeBufferForward: jest.fn().mockResolvedValue(false),
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

    // Forward-interception middleware must be installed before any command handler,
    // so forwarded messages whose text starts with a /command are buffered, not executed.
    expect(bot.use).toHaveBeenCalledTimes(1);
    expect(bot.use.mock.invocationCallOrder[0]).toBeLessThan(bot.command.mock.invocationCallOrder[0]);

    expect(bot.command).toHaveBeenCalledTimes(6);
    expect(bot.command).toHaveBeenNthCalledWith(1, 'start', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(2, 'help', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(3, 'status', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(4, 'cancel', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(5, 'new', expect.any(Function));
    expect(bot.command).toHaveBeenNthCalledWith(6, 'forward', expect.any(Function));
  });
});
