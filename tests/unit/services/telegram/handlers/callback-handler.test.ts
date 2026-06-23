import { CallbackHandler } from '../../../../../src/services/telegram/handlers/callback-handler';

function makeCtx(callbackData: string, userId = 42, chatId = 100) {
  return {
    callbackQuery: {
      data: callbackData,
      message: { text: '⚠️ Confirm: Delete 5 tasks', chat: { id: chatId } },
    },
    from: { id: userId },
    chat: { id: chatId },
    answerCbQuery: jest.fn().mockResolvedValue(undefined),
    editMessageText: jest.fn().mockResolvedValue(undefined),
    reply: jest.fn().mockResolvedValue(undefined),
  } as any;
}

describe('CallbackHandler', () => {
  it('calls resume with the threadId encoded in approve callback data', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'tg_abc_msg123',
        response: 'Done. 5 tasks deleted.',
        toolResults: [],
      }),
    };
    const pendingStore = {
      get: jest.fn(),
      save: jest.fn(),
      clear: jest.fn().mockResolvedValue(undefined),
    };

    const handler = new CallbackHandler(agentClient as any, pendingStore as any);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'approve', threadId: 'tg_abc_msg123' }),
      expect.objectContaining({ threadId: 'tg_abc_msg123' }),
    );
    expect(pendingStore.clear).toHaveBeenCalledWith(expect.any(String), 'completed');
  });

  it('calls resume with the threadId encoded in decline callback data', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'tg_abc_msg123',
        response: 'Action declined — no changes were made.',
        toolResults: [],
      }),
    };
    const pendingStore = {
      get: jest.fn(),
      save: jest.fn(),
      clear: jest.fn().mockResolvedValue(undefined),
    };

    const handler = new CallbackHandler(agentClient as any, pendingStore as any);
    const ctx = makeCtx('confirm:decline:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'decline', threadId: 'tg_abc_msg123' }),
      expect.any(Object),
    );
  });

  it('does nothing for non-confirm callback data', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = { get: jest.fn(), save: jest.fn(), clear: jest.fn() };
    const handler = new CallbackHandler(agentClient as any, pendingStore as any);
    const ctx = makeCtx('some_other_action:data');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Unknown action.');
  });

  it('handles missing or invalid callback data gracefully', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = { get: jest.fn(), save: jest.fn(), clear: jest.fn() };
    const handler = new CallbackHandler(agentClient as any, pendingStore as any);

    // data present but malformed (no threadId after decision)
    const ctx = makeCtx('confirm:approve:');
    await handler.handleCallbackQuery(ctx);
    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Invalid callback data.');
  });
});
