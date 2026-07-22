import { CallbackHandler } from '../../../../../src/services/telegram/handlers/callback-handler';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { buildConversationKey } from '../../../../../src/services/telegram/conversation-key';
import { setRichMessagesEnabled } from '../../../../../src/services/telegram/formatters/telegram-rich';
import { createTerminalReplyStore } from '../../../../../src/services/telegram/terminal-reply.store';

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
) {
  const gateKey = getGateKey(userId, chatId);
  const requestId = `waiting-${threadId}`;
  await gateStore.tryAcquire(gateKey, 60000, undefined, requestId);
  await gateStore.transitionToWaitingIfActiveRequestId(gateKey, requestId, 60000);
  const now = Date.now();
  await pendingStore.save({
    pendingKey: gateKey,
    threadId,
    question: 'Confirm?',
    telegramUserId: userId,
    chatId,
    userId: `telegram:${userId}`,
    requestId,
    interruptType: 'confirm',
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'approve',
        threadId: 'tg_abc_msg123',
        telegramIdentity: {
          telegramId: 42,
          username: 'tester',
        },
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
    expect(ctx.reply).toHaveBeenCalledWith('✅ Approved', { parse_mode: 'MarkdownV2' });
    expect(ctx.editMessageText).not.toHaveBeenCalled();
  });

  it('stores the callback request id while resume is active and compare-clears it afterward', async () => {
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const gateKey = getGateKey();
    let observedRequestId: string | undefined;
    let sentRequestId: string | undefined;
    const agentClient = {
      resume: jest.fn().mockImplementation(async (request: { requestId?: string }) => {
        sentRequestId = request.requestId;
        observedRequestId = await gateStore.getActiveRequestId(gateKey);
        return {
          status: 'completed',
          threadId: 'tg_abc_msg123',
          response: 'Done.',
          toolResults: [],
        };
      }),
    };
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());

    await handler.handleCallbackQuery(makeCtx('confirm:approve:tg_abc_msg123'));

    expect(observedRequestId).toBe(sentRequestId);
    expect(observedRequestId).toMatch(/^cb_/);
    expect(await gateStore.getActiveRequestId(gateKey)).toBeUndefined();
  });

  it('does not release a newer request when an older callback resume settles', async () => {
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const gateKey = getGateKey();
    let resolveResume!: (response: any) => void;
    const resume = jest.fn().mockReturnValue(new Promise((resolve) => {
      resolveResume = resolve;
    }));
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler({ resume } as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    const handling = handler.handleCallbackQuery(ctx);
    while (resume.mock.calls.length === 0) {
      await Promise.resolve();
    }
    const oldRequestId = await gateStore.getActiveRequestId(gateKey);
    expect(oldRequestId).toMatch(/^cb_/);

    expect(await gateStore.releaseIfActiveRequestId(gateKey, oldRequestId!)).toEqual({
      released: true,
      bufferedMessage: undefined,
    });
    expect(await gateStore.tryAcquire(gateKey, 60000)).toBe(true);
    await gateStore.setActiveRequestId(gateKey, 'request-new');

    resolveResume({
      status: 'completed',
      threadId: 'tg_abc_msg123',
      response: 'Old response.',
      toolResults: [],
    });
    await handling;

    expect(await gateStore.getStatus(gateKey)).toBe('running');
    expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-new');
    expect(ctx.reply).not.toHaveBeenCalledWith('Old response.', expect.anything());
    expect(ctx.telegram.deleteMessage).toHaveBeenCalled();
  });

  it('suppresses an older callback error after a newer request acquires the gate', async () => {
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const gateKey = getGateKey();
    let rejectResume!: (error: Error) => void;
    const resume = jest.fn().mockReturnValue(new Promise((_resolve, reject) => {
      rejectResume = reject;
    }));
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler({ resume } as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    const handling = handler.handleCallbackQuery(ctx);
    while (resume.mock.calls.length === 0) await Promise.resolve();
    const oldRequestId = await gateStore.getActiveRequestId(gateKey);
    await gateStore.releaseIfActiveRequestId(gateKey, oldRequestId!);
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-new');

    rejectResume(new Error('old request failed'));
    await handling;

    expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-new');
    expect(ctx.reply.mock.calls.flat().join(' ')).not.toContain('Something went wrong');
    expect(ctx.telegram.deleteMessage).toHaveBeenCalled();
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:decline:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'decline', threadId: 'tg_abc_msg123' }),
      expect.any(Object),
      expect.any(Function),
    );

    expect(ctx.editMessageReplyMarkup).toHaveBeenCalledWith(undefined);
    expect(ctx.reply).toHaveBeenCalledWith('❌ Declined', { parse_mode: 'MarkdownV2' });
    expect(ctx.editMessageText).not.toHaveBeenCalled();
  });

  it.each([
    ['approve', '✅ Approved'],
    ['decline', '❌ Declined'],
  ])('sends the %s acknowledgement as a rich standalone message', async (decision, text) => {
    setRichMessagesEnabled(true);
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'tg_abc_msg123',
        response: '',
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx(`confirm:${decision}:tg_abc_msg123`);
    ctx.telegram.callApi = jest.fn().mockResolvedValue({ message_id: 900 });

    await handler.handleCallbackQuery(ctx);

    expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
      chat_id: 100,
      rich_message: { markdown: text },
    });
    expect(ctx.editMessageReplyMarkup).toHaveBeenCalledWith(undefined);
    expect(ctx.editMessageText).not.toHaveBeenCalled();
  });

  it('falls back to MarkdownV2 when a rich decision acknowledgement fails', async () => {
    setRichMessagesEnabled(true);
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'tg_abc_msg123',
        response: '',
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');
    ctx.telegram.callApi = jest.fn()
      .mockRejectedValueOnce(new Error('rich unsupported'))
      .mockResolvedValue(undefined);

    await handler.handleCallbackQuery(ctx);

    expect(ctx.reply).toHaveBeenCalledWith('✅ Approved', { parse_mode: 'MarkdownV2' });
    expect(agentClient.resume).toHaveBeenCalled();
  });

  it('does nothing for non-confirm callback data', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('some_other_action:data');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Unknown action.');
  });

  it('handles missing or invalid callback data gracefully', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());

    const ctx = makeCtx('confirm:approve:');
    await handler.handleCallbackQuery(ctx);
    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Invalid callback data.');
  });

  it('returns "expired" when pending record does not exist (stale button)', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(ctx.answerCbQuery).toHaveBeenCalledWith('Already processing your decision.');
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('does not resume a newer waiting generation after reading an older pending row', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const gateKey = getGateKey();
    await setupWaitingGate(gateStore, pendingStore);
    const getSnapshot = gateStore.getSnapshot.bind(gateStore);
    const oldSnapshot = await getSnapshot(gateKey);
    gateStore.getSnapshot = jest.fn().mockImplementationOnce(async (key) => {
      await gateStore.releaseIfWaitingRequestId(key, oldSnapshot.requestId);
      await gateStore.tryAcquire(key, 60000, undefined, 'waiting-new');
      await gateStore.transitionToWaitingIfActiveRequestId(key, 'waiting-new', 60000);
      const now = Date.now();
      await pendingStore.save({
        pendingKey: gateKey,
        threadId: 'tg_new_thread',
        question: 'New confirmation?',
        telegramUserId: 42,
        userId: 'telegram:42',
        requestId: 'waiting-new',
        interruptType: 'confirm',
        status: 'pending',
        createdAt: now,
        updatedAt: now,
        expiresAt: now + 60000,
      });
      return getSnapshot(key);
    });
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(await getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'waiting-new',
    });
  });

  it('rejects callback data when the encoded thread does not match the pending record', async () => {
    const agentClient = { resume: jest.fn() };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore, 42, 100, 'tg_user_expected');

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const gateKey = getGateKey();
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
    const pending = await pendingStore.get(gateKey);
    expect(pending).not.toBeNull();
    expect(pending!.threadId).toBe('tg_abc_msg123');
    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: pending!.requestId,
    });
    expect(ctx.reply).toHaveBeenCalledWith(expect.stringContaining('Something went wrong'));
  });

  it('retains callback ownership and pending state when HTTP delivery is ambiguous', async () => {
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'failed',
        delivery: 'ambiguous',
        threadId: 'tg_abc_msg123',
        response: 'This decision may still be running. Use /cancel if you want to stop it.',
        toolResults: [],
        error: 'socket reset',
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const gateKey = getGateKey();
    const snapshot = await gateStore.getSnapshot(gateKey);
    expect(snapshot.status).toBe('running');
    expect(snapshot.requestId).toMatch(/^cb_/);
    expect(await gateStore.getActiveRequestId(gateKey)).toBe(snapshot.requestId);
    expect(await pendingStore.get(gateKey)).toEqual(expect.objectContaining({
      threadId: 'tg_abc_msg123',
      requestId: 'waiting-tg_abc_msg123',
      status: 'pending',
    }));
    expect(ctx.reply.mock.calls.flat().join(' ')).toContain('may still be running');
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const gateKey = getGateKey();
    const pending = await pendingStore.get(gateKey);
    expect(pending).not.toBeNull();
    expect(pending!.interruptType).toBe('clarify');
    expect(pending!.threadId).toBe('tg_abc_msg123');
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
  });

  it('clears a callback re-interrupt saved after its waiting generation was cancelled', async () => {
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
    const committedSave = pendingStore.save.bind(pendingStore);
    jest.spyOn(pendingStore, 'save').mockImplementationOnce(async (record) => {
      const owned = await gateStore.getSnapshot(getGateKey());
      expect(owned.status).toBe('waiting_for_clarification');
      await gateStore.releaseIfWaitingRequestId(getGateKey(), owned.requestId);
      await gateStore.tryAcquire(getGateKey(), 60_000, undefined, 'request-new');
      await committedSave(record);
    });
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());

    await handler.handleCallbackQuery(makeCtx('confirm:approve:tg_abc_msg123'));

    expect(await pendingStore.get(getGateKey())).toBeUndefined();
    expect(await gateStore.getSnapshot(getGateKey())).toEqual({
      status: 'running',
      requestId: 'request-new',
    });
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

    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');

    await handler.handleCallbackQuery(ctx);

    const clarifyReply = (ctx.reply as jest.Mock).mock.calls
      .map((call) => String(call[0]))
      .find((text) => text.includes('Which project do you mean?'));
    expect(clarifyReply).toBe('Which project do you mean?');
    expect(clarifyReply).not.toContain('Clarification required');
  });

  it('persists the rich block id for a callback-generated clarification', async () => {
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
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');
    ctx.telegram.callApi = jest.fn()
      .mockResolvedValueOnce({ message_id: 900 })
      .mockResolvedValueOnce(undefined)
      .mockResolvedValueOnce({ message_id: 901 });

    await handler.handleCallbackQuery(ctx);

    const richCalls = ctx.telegram.callApi.mock.calls.filter(
      (call: unknown[]) => call[0] === 'sendRichMessage',
    );
    expect(richCalls[0][1].rich_message.markdown).toBe('✅ Approved');
    expect(richCalls[1][1].rich_message.markdown).toContain('<details open>');
    const pending = await pendingStore.get(getGateKey());
    expect(pending?.clarificationMessageId).toBe(901);
  });

  it('sends a callback-triggered confirmation re-interrupt with unchanged buttons', async () => {
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
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');
    ctx.telegram.callApi = jest.fn().mockResolvedValue({ message_id: 909 });

    await handler.handleCallbackQuery(ctx);

    const reInterruptPromptIndex = ctx.reply.mock.calls.findIndex(
      (call: unknown[]) => Boolean((call[1] as any)?.reply_markup?.inline_keyboard),
    );
    expect(reInterruptPromptIndex).toBeGreaterThanOrEqual(0);
  });

  it('clears the newly saved pending snapshot when callback prompt delivery fails', async () => {
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
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    jest.spyOn(handler as any, 'sendConfirmReply').mockRejectedValue(new Error('Telegram unavailable'));

    await handler.handleCallbackQuery(makeCtx('confirm:approve:tg_abc_msg123'));

    const gateKey = getGateKey();
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
    expect(await pendingStore.get(gateKey)).toBeUndefined();
  });

  it('exact-clears a new pending row when save commits and then reports failure', async () => {
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
    const committedSave = pendingStore.save.bind(pendingStore);
    jest.spyOn(pendingStore, 'save').mockImplementationOnce(async (record) => {
      await committedSave(record);
      throw new Error('connection lost after commit');
    });
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());

    await handler.handleCallbackQuery(makeCtx('confirm:approve:tg_abc_msg123'));

    const gateKey = getGateKey();
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
    expect(await pendingStore.get(gateKey)).toBeUndefined();
  });

  it('deletes a callback confirmation prompt that loses ownership after delivery', async () => {
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
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');
    ctx.reply.mockImplementation(async (_text: string, options?: any) => {
      if (options?.reply_markup?.inline_keyboard) {
        const owned = await gateStore.getSnapshot(getGateKey());
        await gateStore.releaseIfWaitingRequestId(getGateKey(), owned.requestId);
        await gateStore.tryAcquire(getGateKey(), 60000, undefined, 'request-new');
        await gateStore.transitionToWaitingIfActiveRequestId(getGateKey(), 'request-new', 60000);
        const now = Date.now();
        await pendingStore.save({
          pendingKey: getGateKey(),
          threadId: 'thread-new',
          question: 'New confirmation?',
          telegramUserId: 42,
          userId: 'telegram:42',
          requestId: 'request-new',
          interruptType: 'confirm',
          status: 'pending',
          createdAt: now,
          updatedAt: now,
          expiresAt: now + 60000,
        });
        return { message_id: 919 };
      }
      return { message_id: 88 };
    });

    await handler.handleCallbackQuery(ctx);

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(100, 919);
    expect(await gateStore.getSnapshot(getGateKey())).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'request-new',
    });
  });

  it('deletes a callback clarification prompt that loses ownership after delivery', async () => {
    setRichMessagesEnabled(true);
    const agentClient = {
      resume: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'tg_abc_msg123',
        response: 'Which project?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
    };
    const pendingStore = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    await setupWaitingGate(gateStore, pendingStore);
    const handler = new CallbackHandler(agentClient as any, pendingStore, gateStore, createTerminalReplyStore());
    const ctx = makeCtx('confirm:approve:tg_abc_msg123');
    ctx.telegram.callApi = jest.fn().mockImplementation(async (method: string, payload: any) => {
      if (method === 'sendRichMessage' && String(payload?.rich_message?.markdown).includes('<details open>')) {
        const owned = await gateStore.getSnapshot(getGateKey());
        await gateStore.releaseIfWaitingRequestId(getGateKey(), owned.requestId);
        await gateStore.tryAcquire(getGateKey(), 60000, undefined, 'request-new');
        await gateStore.transitionToWaitingIfActiveRequestId(getGateKey(), 'request-new', 60000);
        const now = Date.now();
        await pendingStore.save({
          pendingKey: getGateKey(),
          threadId: 'thread-new',
          question: 'New clarification?',
          telegramUserId: 42,
          userId: 'telegram:42',
          requestId: 'request-new',
          interruptType: 'clarify',
          status: 'pending',
          createdAt: now,
          updatedAt: now,
          expiresAt: now + 60000,
        });
        return { message_id: 920 };
      }
      return { message_id: 900 };
    });

    await handler.handleCallbackQuery(ctx);

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(100, 920);
    const pending = await pendingStore.get(getGateKey());
    expect(pending?.requestId).toBe('request-new');
    expect(pending?.clarificationMessageId).toBeUndefined();
  });
});
