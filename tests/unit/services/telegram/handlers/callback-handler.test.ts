import { CallbackHandler } from '../../../../../src/services/telegram/handlers/callback-handler';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { buildConversationKey } from '../../../../../src/services/telegram/conversation-key';

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
    editMessageReplyMarkup: jest.fn().mockResolvedValue(undefined),
    reply: jest.fn().mockResolvedValue(undefined),
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
    status: 'pending',
    createdAt: now,
    updatedAt: now,
    expiresAt: now + 30 * 60 * 1000,
  });
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
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore);
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'approve', threadId: 'tg_abc_msg123' }),
      expect.objectContaining({ threadId: 'tg_abc_msg123' }),
      expect.any(Function),
    );
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
});
