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
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.transitionToWaiting(gateKey, 60000);
    await pendingStore.save({
      pendingKey: gateKey,
      threadId: 'thread-1',
      question: 'Which project?',
      telegramUserId: 123,
      chatId: 456,
      userId: 'telegram:123',
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
    const ctx = {
      from: { id: 123, username: 'tester' },
      chat: { id: 456 },
      reply: jest.fn().mockResolvedValue(undefined),
      telegram: { callApi: jest.fn().mockResolvedValue(true) },
    } as any;

    await handlers.handleCancel(ctx);

    expect(await pendingStore.get(gateKey)).toBeUndefined();
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
        markdown: "Conversation cancelled. Let me know what you you'd like to do next!",
      },
    });
    expect(ctx.reply).not.toHaveBeenCalled();
  });
});
