import { CommandHandlers } from '../../../../../src/services/telegram/handlers/command-handlers';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { buildConversationKey } from '../../../../../src/services/telegram/conversation-key';
import { setRichMessagesEnabled } from '../../../../../src/services/telegram/formatters/telegram-rich';

describe('CommandHandlers', () => {
  afterEach(() => {
    setRichMessagesEnabled(false);
  });

  function createContext() {
    return {
      from: { id: 123, username: 'tester' },
      chat: { id: 456 },
      reply: jest.fn().mockResolvedValue(undefined),
      telegram: {
        callApi: jest.fn().mockResolvedValue({ message_id: 1 }),
      },
    } as any;
  }

  function createActivityService() {
    return {
      recordActivity: jest.fn(),
    } as any;
  }

  it('returns help text advertising all commands and no image support', async () => {
    const ctx = createContext();
    const activityService = createActivityService();
    const statusService = {
      getFormattedStatus: jest.fn(),
    } as any;
    const handlers = new CommandHandlers(activityService, statusService, new MemoryConversationGateStore(), new MemoryPendingClarificationStore());

    await handlers.handleHelp(ctx);

    expect(activityService.recordActivity).toHaveBeenCalledWith('command_help');
    const helpText = ctx.reply.mock.calls[0][0] as string;
    for (const command of ['/start', '/help', '/status', '/cancel', '/new']) {
      expect(helpText).toContain(command);
    }
    // Images are no longer accepted, so /help must not offer them as a capability.
    expect(helpText).not.toMatch(/send a photo/i);
    expect(ctx.reply).toHaveBeenCalledWith(expect.any(String), { parse_mode: 'MarkdownV2' });
  });

  it('sends the onboarding welcome message on /start', async () => {
    const ctx = createContext();
    const activityService = createActivityService();
    const statusService = { getFormattedStatus: jest.fn() } as any;
    const handlers = new CommandHandlers(activityService, statusService, new MemoryConversationGateStore(), new MemoryPendingClarificationStore());

    await handlers.handleStart(ctx);

    expect(activityService.recordActivity).toHaveBeenCalledWith('command_start');
    expect(ctx.reply).toHaveBeenCalledWith(
      expect.stringContaining('Jarvis'),
      { parse_mode: 'MarkdownV2' },
    );
  });

  it('returns a formatted healthy status response', async () => {
    const ctx = createContext();
    const activityService = createActivityService();
    const statusService = {
      getFormattedStatus: jest.fn().mockResolvedValue('healthy status'),
    } as any;
    const handlers = new CommandHandlers(activityService, statusService, new MemoryConversationGateStore(), new MemoryPendingClarificationStore());

    await handlers.handleStatus(ctx);

    expect(activityService.recordActivity).toHaveBeenCalledWith('command_status');
    // The requesting user's Telegram id must be threaded through so the backend
    // can check that user's Todoist token.
    expect(statusService.getFormattedStatus).toHaveBeenCalledWith(123);
    expect(ctx.reply).toHaveBeenCalledWith('healthy status', { parse_mode: 'MarkdownV2' });
  });

  it('returns a formatted degraded status response without throwing', async () => {
    const ctx = createContext();
    const activityService = createActivityService();
    const statusService = {
      getFormattedStatus: jest.fn().mockResolvedValue('degraded status'),
    } as any;
    const handlers = new CommandHandlers(activityService, statusService, new MemoryConversationGateStore(), new MemoryPendingClarificationStore());

    await handlers.handleStatus(ctx);

    expect(ctx.reply).toHaveBeenCalledWith('degraded status', { parse_mode: 'MarkdownV2' });
  });

  it.each([
    ['help', 'handleHelp', 'Jarvis'],
    ['status', 'handleStatus', 'healthy status'],
  ])('sends /%s through the rich-message path when enabled', async (_command, method, text) => {
    setRichMessagesEnabled(true);
    const ctx = createContext();
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn().mockResolvedValue('healthy status') } as any,
      new MemoryConversationGateStore(),
      new MemoryPendingClarificationStore(),
    );

    await (handlers[method as 'handleHelp' | 'handleStatus'] as (ctx: any) => Promise<void>).call(
      handlers,
      ctx,
    );

    expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
      chat_id: 456,
      rich_message: { markdown: expect.stringContaining(text) },
    });
    expect(ctx.reply).not.toHaveBeenCalled();
  });

  it('falls back to MarkdownV2 when a rich command response fails', async () => {
    setRichMessagesEnabled(true);
    const ctx = createContext();
    ctx.telegram.callApi.mockRejectedValueOnce(new Error('rich unavailable'));
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      new MemoryConversationGateStore(),
      new MemoryPendingClarificationStore(),
    );

    await handlers.handleHelp(ctx);

    expect(ctx.reply).toHaveBeenCalledWith(
      expect.stringContaining('Jarvis'),
      { parse_mode: 'MarkdownV2' },
    );
  });

  it('deletes and collapses a pending clarification on /cancel', async () => {
    const activityService = createActivityService();
    const statusService = { getFormattedStatus: jest.fn() } as any;
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-waiting');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'request-waiting', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-1',
      question: 'Which project?',
      telegramUserId: 123,
      chatId: 456,
      userId: 'telegram:123',
      requestId: 'request-waiting',
      interruptType: 'clarify',
      clarificationMessageId: 11,
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const handlers = new CommandHandlers(activityService, statusService, gateStore, pendingStore);
    const ctx = {
      from: { id: 123, username: 'tester' },
      chat: { id: 456 },
      reply: jest.fn().mockResolvedValue(undefined),
      telegram: {
        deleteMessage: jest.fn().mockResolvedValue(true),
        callApi: jest.fn().mockResolvedValue(true),
      },
    } as any;

    await handlers.handleCancel(ctx);

    expect(ctx.telegram.callApi).toHaveBeenCalledWith('editMessageText', {
      chat_id: 456,
      message_id: 11,
      rich_message: {
        markdown: '<details><summary>Clarification</summary>\n\nWhich project?\n\n</details>',
      },
    });
    expect(await pendingStore.get(gateKey)).toBeUndefined();
  });

  it('does not release or clear a newer waiting generation after a same-status ABA', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-old');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'request-old', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-old',
      question: 'Old question?',
      telegramUserId: 123,
      userId: 'telegram:123',
      requestId: 'request-old',
      interruptType: 'clarify',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const transitionToRunning = gateStore.transitionToRunning.bind(gateStore);
    gateStore.transitionToRunning = jest.fn().mockImplementation(async (
      key,
      ttlMs,
      requestId,
      expectedWaitingRequestId,
    ) => {
      await gateStore.releaseIfWaitingRequestId(key, 'request-old');
      await gateStore.tryAcquire(key, 60000, undefined, 'request-new');
      await gateStore.transitionToWaitingIfActiveRequestId(key, 'request-new', 60000);
      await pendingStore.save({
        pendingKey: gateKey,
        threadId: 'thread-new',
        question: 'New question?',
        telegramUserId: 123,
        userId: 'telegram:123',
        requestId: 'request-new',
        interruptType: 'clarify',
        status: 'pending',
        createdAt: now + 1,
        updatedAt: now + 1,
        expiresAt: now + 60000,
      });
      return transitionToRunning(key, ttlMs, requestId, expectedWaitingRequestId);
    });
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );

    await handlers.handleCancel(createContext());

    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
    expect(await gateStore.getRequestId(gateKey)).toBe('request-new');
    expect((await pendingStore.get(gateKey))?.requestId).toBe('request-new');
  });

  it('deletes a stored confirmation prompt when cancelling its waiting generation', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-confirm');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'request-confirm', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-confirm',
      question: 'Delete the task?',
      telegramUserId: 123,
      userId: 'telegram:123',
      requestId: 'request-confirm',
      interruptType: 'confirm',
      promptMessageId: 77,
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );
    const ctx = createContext();
    ctx.telegram.deleteMessage = jest.fn().mockResolvedValue(true);

    await handlers.handleCancel(ctx);

    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
    expect(await pendingStore.get(gateKey)).toBeUndefined();
  });

  it('releases a waiting gate and cleans its prompt after the pending read TTL elapses', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-expired');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'request-expired', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-expired',
      question: 'Delete the task?',
      telegramUserId: 123,
      chatId: 456,
      userId: 'telegram:123',
      requestId: 'request-expired',
      interruptType: 'confirm',
      promptMessageId: 77,
      status: 'pending',
      createdAt: now - 60000,
      updatedAt: now - 60000,
      expiresAt: now - 1,
    });
    const expireIfMatches = jest.spyOn(pendingStore, 'expireIfMatches');
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );
    const ctx = createContext();
    ctx.telegram.deleteMessage = jest.fn().mockResolvedValue(true);

    await handlers.handleCancel(ctx);

    expect(expireIfMatches).toHaveBeenCalledWith(gateKey, { requestId: 'request-expired' });
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
    expect(ctx.telegram.deleteMessage).toHaveBeenCalledWith(456, 77);
  });

  it('claims the exact waiting generation before reading and clearing its pending row', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-waiting');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'request-waiting', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-waiting',
      question: 'Continue?',
      telegramUserId: 123,
      userId: 'telegram:123',
      requestId: 'request-waiting',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const events: string[] = [];
    const transitionToRunning = gateStore.transitionToRunning.bind(gateStore);
    jest.spyOn(gateStore, 'transitionToRunning').mockImplementation(async (...args) => {
      const transitioned = await transitionToRunning(...args);
      events.push(`claim:${transitioned}`);
      return transitioned;
    });
    const get = pendingStore.get.bind(pendingStore);
    jest.spyOn(pendingStore, 'get').mockImplementation(async (key) => {
      events.push(`read:${(await gateStore.getSnapshot(gateKey)).status}`);
      return get(key);
    });
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );

    await handlers.handleCancel(createContext());

    expect(events.slice(0, 2)).toEqual(['claim:true', 'read:running']);
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
    expect(await pendingStore.get(gateKey)).toBeUndefined();
  });

  it('restores a waiting generation without clearing it when the pending read fails', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-waiting');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'request-waiting', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-waiting',
      question: 'Continue?',
      telegramUserId: 123,
      userId: 'telegram:123',
      requestId: 'request-waiting',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const get = pendingStore.get.bind(pendingStore);
    jest.spyOn(pendingStore, 'get')
      .mockRejectedValueOnce(new Error('temporary database failure'))
      .mockImplementation(get);
    const clearIfMatches = jest.spyOn(pendingStore, 'clearIfMatches');
    const expireIfMatches = jest.spyOn(pendingStore, 'expireIfMatches');
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );

    await handlers.handleCancel(createContext());

    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'request-waiting',
    });
    expect((await pendingStore.get(gateKey))?.requestId).toBe('request-waiting');
    expect(clearIfMatches).not.toHaveBeenCalled();
    expect(expireIfMatches).not.toHaveBeenCalled();
  });

  it('uses the initial waiting snapshot token when the generation changes before pending lookup', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-old');
    await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'request-old', 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-old',
      question: 'Old question?',
      telegramUserId: 123,
      userId: 'telegram:123',
      requestId: 'request-old',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const getSnapshot = gateStore.getSnapshot.bind(gateStore);
    gateStore.getSnapshot = jest.fn().mockImplementationOnce(async (key) => {
      const oldSnapshot = await getSnapshot(key);
      await gateStore.releaseIfWaitingRequestId(key, 'request-old');
      await gateStore.tryAcquire(key, 60000, undefined, 'request-new');
      await gateStore.transitionToWaitingIfActiveRequestId(key, 'request-new', 60000);
      await pendingStore.save({
        pendingKey: gateKey,
        threadId: 'thread-new',
        question: 'New question?',
        telegramUserId: 123,
        userId: 'telegram:123',
        requestId: 'request-new',
        status: 'pending',
        createdAt: now + 1,
        updatedAt: now + 1,
        expiresAt: now + 60000,
      });
      return oldSnapshot;
    });
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );

    await handlers.handleCancel(createContext());

    expect(await getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'request-new',
    });
    expect((await pendingStore.get(gateKey))?.requestId).toBe('request-new');
  });

  it('clears a leftover pending clarification on /cancel even when the gate is idle', async () => {
    const activityService = createActivityService();
    const statusService = { getFormattedStatus: jest.fn() } as any;
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    // No tryAcquire/transitionToWaiting: the gate stays idle, but a stale pending row lingers.
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-1',
      question: 'Which project?',
      telegramUserId: 123,
      chatId: 456,
      userId: 'telegram:123',
      interruptType: 'clarify',
      status: 'pending',
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    });
    const handlers = new CommandHandlers(activityService, statusService, gateStore, pendingStore);
    const release = jest.spyOn(gateStore, 'release');
    const ctx = {
      from: { id: 123, username: 'tester' },
      chat: { id: 456 },
      reply: jest.fn().mockResolvedValue(undefined),
      telegram: { callApi: jest.fn().mockResolvedValue(true) },
    } as any;

    await handlers.handleCancel(ctx);

    expect(await pendingStore.get(gateKey)).toBeUndefined();
    expect(release).not.toHaveBeenCalled();
  });

  it('claims an expired pending prompt in the idle-before-expiry-timer gap', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-expired',
      question: 'Choose a project',
      telegramUserId: 123,
      chatId: 456,
      userId: 'telegram:123',
      requestId: 'request-expired',
      interruptType: 'clarify',
      clarificationMessageId: 88,
      promptMessageId: 88,
      status: 'pending',
      createdAt: now - 60000,
      updatedAt: now - 60000,
      expiresAt: now - 1,
    });
    jest.spyOn(gateStore, 'getSnapshot').mockResolvedValueOnce({
      status: 'idle',
      requestId: 'request-expired',
    });
    const expireIfMatches = jest.spyOn(pendingStore, 'expireIfMatches');
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );
    const ctx = createContext();

    await handlers.handleCancel(ctx);

    expect(expireIfMatches).toHaveBeenCalledWith(gateKey, { requestId: 'request-expired' });
    expect(ctx.telegram.callApi).toHaveBeenCalledWith('editMessageText', {
      chat_id: 456,
      message_id: 88,
      rich_message: {
        markdown: '<details><summary>Clarification</summary>\n\nChoose a project\n\n</details>',
      },
    });
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
  });

  it('does not clear a pending row created after an initial idle cancel snapshot', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    const now = Date.now();
    const originalTryAcquire = gateStore.tryAcquire.bind(gateStore);
    gateStore.tryAcquire = jest.fn().mockImplementationOnce(async (key, ttlMs, chatId, _requestId) => {
      await originalTryAcquire(key, ttlMs, chatId, 'request-new');
      await gateStore.transitionToWaitingIfActiveRequestId(key, 'request-new', 60000);
      await pendingStore.save({
        pendingKey: key,
        threadId: 'thread-new',
        question: 'New question?',
        telegramUserId: 123,
        userId: 'telegram:123',
        requestId: 'request-new',
        status: 'pending',
        createdAt: now,
        updatedAt: now,
        expiresAt: now + 60000,
      });
      return false;
    });
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
    );
    const ctx = createContext();

    await handlers.handleCancel(ctx);

    expect(await gateStore.getSnapshot(gateKey)).toEqual({
      status: 'waiting_for_clarification',
      requestId: 'request-new',
    });
    expect((await pendingStore.get(gateKey))?.requestId).toBe('request-new');
    expect(ctx.reply.mock.calls[0][0]).toContain('another request is active');
  });

  it('awaits backend cancellation before releasing a running gate', async () => {
    const gateStore = new MemoryConversationGateStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.setActiveRequestId(gateKey, 'request-1');
    let resolveCancel!: (outcome: 'cancelled') => void;
    const cancelRun = jest.fn().mockReturnValue(
      new Promise<'cancelled'>((resolve) => { resolveCancel = resolve; }),
    );
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      new MemoryPendingClarificationStore(),
      { cancelRun } as any,
    );
    const ctx = createContext();

    const cancelling = handlers.handleCancel(ctx);
    await Promise.resolve();
    await Promise.resolve();
    expect(await gateStore.getStatus(gateKey)).toBe('running');

    resolveCancel('cancelled');
    await cancelling;

    expect(cancelRun).toHaveBeenCalledWith('telegram:123', 'request-1');
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
    expect(ctx.reply).toHaveBeenCalledWith(
      expect.stringContaining('Conversation cancelled'),
      { parse_mode: 'MarkdownV2' },
    );
  });

  it('does not release a newer request when a delayed cancellation settles', async () => {
    const gateStore = new MemoryConversationGateStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.setActiveRequestId(gateKey, 'request-1');
    let resolveCancel!: (outcome: 'cancelled') => void;
    const cancelRun = jest.fn().mockReturnValue(
      new Promise<'cancelled'>((resolve) => { resolveCancel = resolve; }),
    );
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      new MemoryPendingClarificationStore(),
      { cancelRun } as any,
    );
    const ctx = createContext();

    const cancelling = handlers.handleCancel(ctx);
    while (cancelRun.mock.calls.length === 0) {
      await Promise.resolve();
    }

    expect(await gateStore.releaseIfActiveRequestId(gateKey, 'request-1')).toEqual({
      released: true,
      bufferedMessage: undefined,
    });
    expect(await gateStore.tryAcquire(gateKey, 60000)).toBe(true);
    await gateStore.setActiveRequestId(gateKey, 'request-2');

    resolveCancel('cancelled');
    await cancelling;

    expect(await gateStore.getStatus(gateKey)).toBe('running');
    expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-2');
    expect(ctx.reply.mock.calls[0][0]).toContain('another request is active');
  });

  it('retains a running gate while a confirmed mutation is in flight', async () => {
    const gateStore = new MemoryConversationGateStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.setActiveRequestId(gateKey, 'request-1');
    const cancelRun = jest.fn().mockResolvedValue('mutation_in_flight');
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      new MemoryPendingClarificationStore(),
      { cancelRun } as any,
    );
    const ctx = createContext();

    await handlers.handleCancel(ctx);

    expect(await gateStore.getStatus(gateKey)).toBe('running');
    expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-1');
    expect(ctx.reply.mock.calls[0][0]).toContain("can't safely cancel");
  });

  it('retains a running gate when the backend cannot confirm the request exists', async () => {
    const gateStore = new MemoryConversationGateStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-1');
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      new MemoryPendingClarificationStore(),
      { cancelRun: jest.fn().mockResolvedValue('not_found') } as any,
    );
    const ctx = createContext();

    await handlers.handleCancel(ctx);

    expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-1');
    expect(ctx.reply.mock.calls[0][0]).toContain("couldn't confirm cancellation");
  });

  it('retains a running gate when the backend has finished before Telegram settlement', async () => {
    const gateStore = new MemoryConversationGateStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-1');
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      new MemoryPendingClarificationStore(),
      { cancelRun: jest.fn().mockResolvedValue('already_finished') } as any,
    );

    await handlers.handleCancel(createContext());

    expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-1');
    expect(await gateStore.getStatus(gateKey)).toBe('running');
  });

  it('does not clear a newer pending row created after the owned gate is released', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-old');
    const now = Date.now();
    const oldPending = {
      pendingKey: gateKey,
      threadId: 'thread-old',
      question: 'Old question?',
      telegramUserId: 123,
      userId: 'telegram:123',
      requestId: 'request-old',
      interruptType: 'clarify' as const,
      status: 'pending' as const,
      createdAt: now,
      updatedAt: now,
      expiresAt: now + 60000,
    };
    await pendingStore.save(oldPending);
    const clearIfMatches = pendingStore.clearIfMatches.bind(pendingStore);
    pendingStore.clearIfMatches = jest.fn().mockImplementation(async (...args: any[]) => {
      await pendingStore.save({
        ...oldPending,
        threadId: 'thread-new',
        requestId: 'request-new',
        question: 'New question?',
      });
      return clearIfMatches(...(args as Parameters<typeof clearIfMatches>));
    });
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      pendingStore,
      { cancelRun: jest.fn().mockResolvedValue('cancelled') } as any,
    );

    await handlers.handleCancel(createContext());

    expect((await pendingStore.get(gateKey))?.requestId).toBe('request-new');
  });

  it('retains a running gate when backend cancellation fails', async () => {
    const gateStore = new MemoryConversationGateStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.setActiveRequestId(gateKey, 'request-1');
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      new MemoryPendingClarificationStore(),
      { cancelRun: jest.fn().mockRejectedValue(new Error('network unavailable')) } as any,
    );
    const ctx = createContext();

    await handlers.handleCancel(ctx);

    expect(await gateStore.getStatus(gateKey)).toBe('running');
    expect(ctx.reply.mock.calls[0][0]).toContain("couldn't confirm cancellation");
  });

  it('retains a running gate when no active backend request id is available', async () => {
    const gateStore = new MemoryConversationGateStore();
    const gateKey = buildConversationKey(123, 'telegram:123', 456);
    await gateStore.tryAcquire(gateKey, 60000);
    const cancelRun = jest.fn();
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      gateStore,
      new MemoryPendingClarificationStore(),
      { cancelRun } as any,
    );
    const ctx = createContext();

    await handlers.handleCancel(ctx);

    expect(cancelRun).not.toHaveBeenCalled();
    expect(await gateStore.getStatus(gateKey)).toBe('running');
  });

  it('sends the /cancel confirmation through the rich-message path when enabled', async () => {
    setRichMessagesEnabled(true);
    const ctx = createContext();
    const handlers = new CommandHandlers(
      createActivityService(),
      { getFormattedStatus: jest.fn() } as any,
      new MemoryConversationGateStore(),
      new MemoryPendingClarificationStore(),
    );

    await handlers.handleCancel(ctx);

    expect(ctx.telegram.callApi).toHaveBeenCalledWith('sendRichMessage', {
      chat_id: 456,
      rich_message: {
        markdown: "Conversation cancelled. Let me know what you'd like to do next!",
      },
    });
    expect(ctx.reply).not.toHaveBeenCalled();
  });
});
