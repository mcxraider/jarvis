import { CallbackHandler } from '../../../../../src/services/telegram/handlers/callback-handler';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { buildConversationKey } from '../../../../../src/services/telegram/conversation-key';
import { setRichMessagesEnabled } from '../../../../../src/services/telegram/formatters/telegram-rich';

function makeCtx(callbackData: string, userId = 42, chatId = 100) {
  return {
    callbackQuery: {
      data: callbackData,
      message: { text: '⚠️ Confirm: Delete 5 tasks', chat: { id: chatId } },
    },
    from: { id: userId, username: 'tester', first_name: 'Test' },
    chat: { id: chatId },
    answerCbQuery: jest.fn().mockResolvedValue(undefined),
    editMessageText: jest.fn().mockResolvedValue(undefined),
    editMessageReplyMarkup: jest.fn().mockResolvedValue(undefined),
    reply: jest.fn().mockResolvedValue({ message_id: 88 }),
    telegram: {
      deleteMessage: jest.fn().mockResolvedValue(true),
    },
  } as any;
}

function getGateKey(userId = 42, chatId = 100): string {
  const internalUserId = `telegram:${userId}`;
  return buildConversationKey(userId, internalUserId, chatId);
}

async function setupWaitingGate(
  gateStore: MemoryConversationGateStore,
  pendingStore: MemoryPendingClarificationStore,
  userId = 42,
  chatId = 100,
  threadId = 'tg_abc_msg123',
  awaitingMessageId?: number,
) {
  const gateKey = getGateKey(userId, chatId);
  await gateStore.tryAcquire(gateKey, 60000);
  await gateStore.transitionToWaiting(gateKey, 60000);
  const now = Date.now();
  await pendingStore.save({
    pendingKey: gateKey,
    threadId,
    question: 'Confirm?',
    telegramUserId: userId,
    chatId,
    userId: `telegram:${userId}`,
    interruptType: 'confirm',
    awaitingMessageId,
    status: 'pending',
    createdAt: now,
    updatedAt: now,
    expiresAt: now + 30 * 60 * 1000,
  });
}

describe('CallbackHandler', () => {
  // Rich-mode enablement is module-level state, so reset it between cases.
  afterEach(() => {
    setRichMessagesEnabled(false);
    jest.restoreAllMocks();
  });

  it('calls resume with the threadId encoded in approve callback data', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'tg_abc_msg123',
        response: 'Done. 5 tasks deleted.',
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'approve',
        threadId: 'tg_abc_msg123',
        telegramUsername: 'tester',
        telegramFirstName: 'Test',
      }),
      expect.objectContaining({
        threadId: 'tg_abc_msg123',
        telegramUsername: 'tester',
        telegramFirstName: 'Test',
      }),
      expect.any(Function),
    );

    // The decision is delivered as its own new message, and the confirm message keeps
    // its text (only its inline keyboard is stripped).
    expect(ctx.editMessageReplyMarkup).toHaveBeenCalledWith(undefined);
    expect(ctx.reply).toHaveBeenCalledWith('✅ Approved');
    expect(ctx.editMessageText).not.toHaveBeenCalled();
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
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:decline:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'decline', threadId: 'tg_abc_msg123' }),
      expect.any(Object),
      expect.any(Function),
    );

    expect(ctx.editMessageReplyMarkup).toHaveBeenCalledWith(undefined);
    expect(ctx.reply).toHaveBeenCalledWith('❌ Declined');
    expect(ctx.editMessageText).not.toHaveBeenCalled();
  });

  it('does nothing for non-confirm callback data', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('some_other_action:data');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Unknown action.');
  });

  it('handles missing or invalid callback data gracefully', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);

    const ctx = makeCtx('confirm:approve:');
    await handler.handleCallbackQuery(ctx);
    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Invalid callback data.');
  });

  it('returns "expired" when pending record does not exist (stale button)', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(ctx.answerCbQuery).toHaveBeenCalledWith('This action has expired.');
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('returns "already processing" when transitionToRunning fails (double-tap)', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    // Simulate race: first caller already transitioned
    const gateKey = getGateKey();
    await gateStore.transitionToRunning(gateKey, 60000);
    // Re-save pending so the stale check passes
    const now = Date.now();
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'tg_abc_msg123',
      question: 'Confirm?',
      telegramUserId: 42,
      chatId: 100,
      userId: 'telegram:42',
      interruptType: 'confirm',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 30 * 60 * 1000,
    });

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Already processing your decision.');
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('rejects callback data when the encoded thread does not match the pending record', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore, 42, 100, 'tg_user_expected');

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_user_other');

    await handler.handleCallbackQuery(ctx);

    expect(ctx.answerCbQuery).toHaveBeenCalledWith('This action is not available.');
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('rejects callback data when the pending record belongs to another Telegram user', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const gateKey = getGateKey(42, 100);
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.transitionToWaiting(gateKey, 60000);
    const now = Date.now();
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'tg_user_expected',
      question: 'Confirm?',
      telegramUserId: 43,
      chatId: 100,
      userId: 'telegram:43',
      interruptType: 'confirm',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 30 * 60 * 1000,
    });

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_user_expected', 42, 100);

    await handler.handleCallbackQuery(ctx);

    expect(ctx.answerCbQuery).toHaveBeenCalledWith('This action is not available.');
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('preserves pending record and transitions gate to waiting on resume failure', async () => {
    const agentClient = {
      resume: jest.fn().mockRejectedValue(new Error('network failure')),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const gateKey = getGateKey();
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
    const pending = await pendingStore.get(gateKey);
    expect(pending).not.toBeNull();
    expect(pending!.threadId).toBe('tg_abc_msg123');
    expect(ctx.reply).toHaveBeenCalledWith(expect.stringContaining('Something went wrong'));
  });

  it('clears pending record only after successful resume', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'tg_abc_msg123',
        response: 'Done.',
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const gateKey = getGateKey();
    const pending = await pendingStore.get(gateKey);
    expect(pending).toBeUndefined();
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
  });

  it('saves pending record with clarify type when resume returns a clarify interrupt', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'tg_abc_msg123',
        response: 'Which project do you mean?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const gateKey = getGateKey();
    const pending = await pendingStore.get(gateKey);
    expect(pending).not.toBeNull();
    expect(pending!.interruptType).toBe('clarify');
    expect(pending!.threadId).toBe('tg_abc_msg123');
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
  });

  it('sends a callback-generated clarification verbatim without a redundant header', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'tg_abc_msg123',
        response: 'Which project do you mean?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const clarifyReply = (ctx.reply as jest.Mock).mock.calls
      .map((call) => String(call[0]))
      .find((text) => text.includes('Which project do you mean?'));
    expect(clarifyReply).toBe('Which project do you mean?');
    expect(clarifyReply).not.toContain('Clarification required');
    const pending = await pendingStore.get(getGateKey());
    expect(pending?.awaitingMessageId).toBe(88);
  });

  it('persists both rich message ids for a callback-generated clarification', async () => {
    setRichMessagesEnabled(true);
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'tg_abc_msg123',
        response: 'Which project do you mean?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');
    ctx.telegram.callApi = jest.fn()
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce({ message_id: 901 })
      .mockResolvedValueOnce({ message_id: 902 });

    await handler.handleCallbackQuery(ctx);

    const richCalls = ctx.telegram.callApi.mock.calls.filter(
      (call: unknown[]) => call[0] === 'sendRichMessage',
    );
    expect(richCalls[0][1].rich_message.markdown).toContain('<details open>');
    expect(richCalls[1][1].rich_message.markdown).toBe('⏳ _Awaiting clarification…_');
    const pending = await pendingStore.get(getGateKey());
    expect(pending?.clarificationMessageId).toBe(901);
    expect(pending?.awaitingMessageId).toBe(902);
  });

  it.each(['approve', 'decline'])(
    'deletes a legacy confirmation indicator on %s',
    async (decision) => {
      const agentClient = {
        resume: jest.fn().mockResolvedValue({
          status: 'completed',
          threadId: 'tg_abc_msg123',
          response: 'Done.',
          toolResults: [],
        }),
      };
      const pendingStore = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      await setupWaitingGate(gateStore, pendingStore, 42, 100, 'tg_abc_msg123', 777);

      const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
      const ctx = makeCtx(`confirm:${decision}:tg_abc_msg123`);

      await handler.handleCallbackQuery(ctx);

      expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(100, 777);
      expect(ctx.telegram.deleteMessage.mock.invocationCallOrder[0]).toBeLessThan(
        agentClient.resume.mock.invocationCallOrder[0],
      );
    },
  );

  it('does not delete an indicator when the pending record has none', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'tg_abc_msg123',
        response: 'Done.',
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore); // no awaitingMessageId

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    // The 777 indicator id was never stored, so it must never be deleted.
    expect(ctx.telegram.deleteMessage).not.toHaveBeenCalledWith(100, 777);
  });

  it('does not record a fresh indicator when the resume re-interrupts for confirmation', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'tg_abc_msg123',
        response: 'Also delete the project?',
        interrupt: { type: 'confirm' },
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore, 42, 100, 'tg_abc_msg123', 777);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    // A legacy indicator is still cleaned up, but the new confirmation pause has none.
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(100, 777);
    const pending = await pendingStore.get(getGateKey());
    expect(pending?.awaitingMessageId).toBeUndefined();
  });

  it('sends a callback-triggered confirmation re-interrupt without an Awaiting message', async () => {
    setRichMessagesEnabled(true);
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'tg_abc_msg123',
        response: 'Also delete the project?',
        interrupt: { type: 'confirm' },
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');
    ctx.telegram.callApi = jest.fn().mockResolvedValue({ message_id: 909 });

    await handler.handleCallbackQuery(ctx);

    const reInterruptPromptIndex = ctx.reply.mock.calls.findIndex(
      (call: unknown[]) => Boolean((call[1] as any)?.reply_markup?.inline_keyboard),
    );
    expect(reInterruptPromptIndex).toBeGreaterThanOrEqual(0);
    expect(ctx.telegram.callApi.mock.calls).not.toContainEqual([
      'sendRichMessage',
      expect.objectContaining({
        rich_message: expect.objectContaining({
          markdown: expect.stringContaining('Awaiting'),
        }),
      }),
    ]);
    const pending = await pendingStore.get(getGateKey());
    expect(pending?.awaitingMessageId).toBeUndefined();
  });
});
