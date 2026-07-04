import {
  isRichMessagesEnabled,
  sendFinalReply,
  sendRichMessageToChat,
  setRichMessagesEnabled,
} from '../../../../../src/services/telegram/formatters/telegram-rich';

describe('telegram-rich sendFinalReply', () => {
  afterEach(() => setRichMessagesEnabled(false));

  function createContext(callApi?: jest.Mock) {
    return {
      chat: { id: 123 },
      reply: jest.fn().mockResolvedValue({ message_id: 1 }),
      telegram: { callApi: callApi ?? jest.fn().mockResolvedValue({ message_id: 9 }) },
    } as any;
  }

  it('defaults to disabled and uses the MarkdownV2 reply path', async () => {
    expect(isRichMessagesEnabled()).toBe(false);
    const callApi = jest.fn();
    const ctx = createContext(callApi);

    await sendFinalReply(ctx, '**Done.**');

    expect(callApi).not.toHaveBeenCalled();
    expect(ctx.reply).toHaveBeenCalledWith('*Done\\.*', { parse_mode: 'MarkdownV2' });
  });

  it('sends a rich message with the markdown field when enabled', async () => {
    setRichMessagesEnabled(true);
    const callApi = jest.fn().mockResolvedValue({ message_id: 9 });
    const ctx = createContext(callApi);

    await sendFinalReply(ctx, '## Heading\n- item');

    expect(callApi).toHaveBeenCalledWith('sendRichMessage', {
      chat_id: 123,
      rich_message: { markdown: '## Heading\n- item' },
    });
    expect(ctx.reply).not.toHaveBeenCalled();
  });

  it('falls back to the MarkdownV2 reply path when the rich call fails', async () => {
    setRichMessagesEnabled(true);
    const callApi = jest.fn().mockRejectedValue(new Error('400 Bad Request'));
    const ctx = createContext(callApi);

    await sendFinalReply(ctx, '**Done.**');

    expect(callApi).toHaveBeenCalledTimes(1);
    expect(ctx.reply).toHaveBeenCalledWith('*Done\\.*', { parse_mode: 'MarkdownV2' });
  });
});

describe('telegram-rich sendRichMessageToChat', () => {
  afterEach(() => setRichMessagesEnabled(false));

  const TIMEOUT_TEXT = '⏱ Request timed out. Send a new message to try again.';

  function createTelegram(overrides: { callApi?: jest.Mock; sendMessage?: jest.Mock } = {}) {
    return {
      callApi: overrides.callApi ?? jest.fn().mockResolvedValue({ message_id: 9 }),
      sendMessage: overrides.sendMessage ?? jest.fn().mockResolvedValue({ message_id: 1 }),
    } as any;
  }

  it('sends a rich message by chat id when enabled', async () => {
    setRichMessagesEnabled(true);
    const telegram = createTelegram();

    await sendRichMessageToChat(telegram, 123, TIMEOUT_TEXT);

    expect(telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
      chat_id: 123,
      rich_message: { markdown: TIMEOUT_TEXT },
    });
    expect(telegram.sendMessage).not.toHaveBeenCalled();
  });

  it('uses the MarkdownV2 sendMessage path when disabled', async () => {
    expect(isRichMessagesEnabled()).toBe(false);
    const telegram = createTelegram();

    await sendRichMessageToChat(telegram, 123, TIMEOUT_TEXT);

    expect(telegram.callApi).not.toHaveBeenCalled();
    // The text is MarkdownV2-escaped (note the escaped '.') and sent with parse_mode.
    expect(telegram.sendMessage).toHaveBeenCalledWith(
      123,
      '⏱ Request timed out\\. Send a new message to try again\\.',
      { parse_mode: 'MarkdownV2' },
    );
  });

  it('falls back to sendMessage when the rich call fails', async () => {
    setRichMessagesEnabled(true);
    const callApi = jest.fn().mockRejectedValue(new Error('400 Bad Request'));
    const sendMessage = jest.fn().mockResolvedValue({ message_id: 1 });
    const telegram = createTelegram({ callApi, sendMessage });

    await sendRichMessageToChat(telegram, 123, TIMEOUT_TEXT);

    expect(callApi).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledWith(
      123,
      '⏱ Request timed out\\. Send a new message to try again\\.',
      { parse_mode: 'MarkdownV2' },
    );
  });
});
