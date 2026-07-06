import { TextProcessorService } from '../../../../../src/services/telegram/processors/text-processor.service';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { buildConversationKey } from '../../../../../src/services/telegram/conversation-key';

describe('TextProcessorService', () => {
  const originalTelegramUserMap = process.env.TELEGRAM_USER_MAP;
  const originalTelegramPendingTtlMs = process.env.TELEGRAM_PENDING_TTL_MS;

  afterEach(() => {
    if (originalTelegramUserMap === undefined) {
      delete process.env.TELEGRAM_USER_MAP;
    } else {
      process.env.TELEGRAM_USER_MAP = originalTelegramUserMap;
    }
    if (originalTelegramPendingTtlMs === undefined) {
      delete process.env.TELEGRAM_PENDING_TTL_MS;
    } else {
      process.env.TELEGRAM_PENDING_TTL_MS = originalTelegramPendingTtlMs;
    }
    jest.restoreAllMocks();
  });

  function createService(
    agentClient: unknown,
    store = new MemoryPendingClarificationStore(),
    gateStore = new MemoryConversationGateStore(),
  ): TextProcessorService {
    return new TextProcessorService(agentClient as any, store, gateStore);
  }

  it('invokes the Python agent and returns the final response', async () => {
    process.env.TELEGRAM_USER_MAP = '701122767:jerry';
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Done.',
        toolResults: [],
      }),
      resume: jest.fn(),
    };
    const service = createService(agentClient);

    await expect(
      service.processTextMessage(' \n add milk \t ', 701122767, {
        requestId: 'tg_test',
        chatId: 555,
        messageId: 42,
        telegramUsername: 'jerry',
        telegramFirstName: 'Jerry',
      }),
    ).resolves.toHaveProperty('response', 'Done.');

    expect(agentClient.invoke).toHaveBeenCalledWith(
      {
        message: 'add milk',
        userId: 'jerry',
        source: 'telegram',
        telegramIdentity: {
          telegramId: 701122767,
          username: 'jerry',
        },
        requestId: 'tg_test',
        threadId: 'tg_tg_test',
      },
      {
        requestId: 'tg_test',
        chatId: 555,
        messageId: 42,
        telegramUsername: 'jerry',
        telegramFirstName: 'Jerry',
        threadId: 'tg_tg_test',
      },
    );
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('prepends reply context to fresh agent requests after normalizing the new text', async () => {
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Done.',
        toolResults: [],
      }),
      resume: jest.fn(),
    };
    const service = createService(agentClient);
    const replyContext = '[In reply to your earlier message: "Created task: Buy milk"]';

    await service.processTextMessage(
      ' \n add a due date of tomorrow \t ',
      42,
      { requestId: 'req-reply', chatId: 100 },
      undefined,
      { replyContext },
    );

    expect(agentClient.invoke).toHaveBeenCalledWith(
      expect.objectContaining({
        message: `${replyContext}\n\nadd a due date of tomorrow`,
      }),
      expect.any(Object),
    );
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('rejects whitespace-only text without calling or acquiring resources for the agent', async () => {
    const agentClient = {
      invoke: jest.fn(),
      resume: jest.fn(),
    };
    const gateStore = new MemoryConversationGateStore();
    const getStatus = jest.spyOn(gateStore, 'getStatus');
    const tryAcquire = jest.spyOn(gateStore, 'tryAcquire');
    const service = createService(agentClient, undefined, gateStore);

    await expect(service.processTextMessage(' \n\t ', 42, { chatId: 100 })).resolves.toEqual({
      response: 'Please send a message with some text.',
    });

    expect(getStatus).not.toHaveBeenCalled();
    expect(tryAcquire).not.toHaveBeenCalled();
    expect(agentClient.invoke).not.toHaveBeenCalled();
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('builds a fresh thread id per invocation based on requestId', async () => {
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Done.',
        toolResults: [],
      }),
      resume: jest.fn(),
    };
    const service = createService(agentClient);

    await service.processTextMessage('add milk', 701122767, { requestId: 'req_1', chatId: 555, messageId: 42 });
    await service.processTextMessage('add eggs', 701122767, { requestId: 'req_2', chatId: 555, messageId: 43 });

    const threadIds = agentClient.invoke.mock.calls.map(([request]: [any]) => request.threadId);
    expect(threadIds).toEqual(['tg_req_1', 'tg_req_2']);
    expect(threadIds[0]).not.toBe(threadIds[1]);
  });

  it('generates a UUID-based thread id when requestId is absent', async () => {
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Done.',
        toolResults: [],
      }),
      resume: jest.fn(),
    };
    const service = createService(agentClient);

    await service.processTextMessage('add milk', 701122767, { chatId: 555, messageId: 42 });
    await service.processTextMessage('add milk', 701122768, { chatId: 555, messageId: 43 });

    const threadIds = agentClient.invoke.mock.calls.map(([request]: [any]) => request.threadId);
    expect(threadIds[0]).toMatch(/^tg_/);
    expect(threadIds[1]).toMatch(/^tg_/);
    expect(threadIds[0]).not.toBe(threadIds[1]);
  });

  it('does not create a new thread id while a graph run has not completed', async () => {
    const gateStore = new MemoryConversationGateStore();
    const pendingStore = new MemoryPendingClarificationStore();
    const runningAgentClient = {
      invoke: jest.fn(),
      resume: jest.fn(),
    };
    const runningService = createService(runningAgentClient, pendingStore, gateStore);

    const { buildConversationKey } = require('../../../../../src/services/telegram/conversation-key');
    const gateKey = buildConversationKey(42, 'telegram:42', 100);
    await gateStore.tryAcquire(gateKey, 60000);

    const blockedResult = await runningService.processTextMessage('second request', 42, {
      chatId: 100,
      messageId: 11,
    });

    expect(blockedResult.blocked).toBe(true);
    expect(runningAgentClient.invoke).not.toHaveBeenCalled();
    expect(runningAgentClient.resume).not.toHaveBeenCalled();

    await gateStore.release(gateKey);

    const interruptedAgentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-hitl',
        response: 'Which task should I update?',
        interrupt: { type: 'clarify', question: 'Which task should I update?' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-hitl',
        response: 'Updated the dentist task.',
        toolResults: [],
      }),
    };
    const interruptedService = createService(interruptedAgentClient);

    await interruptedService.processTextMessage('update my task', 42, {
      chatId: 100,
      messageId: 20,
    });
    await interruptedService.processTextMessage('the dentist one', 42, {
      chatId: 100,
      messageId: 21,
    });

    expect(interruptedAgentClient.invoke).toHaveBeenCalledWith(
      expect.objectContaining({ threadId: expect.stringMatching(/^tg_/) }),
      expect.objectContaining({ threadId: expect.stringMatching(/^tg_/) }),
    );
    expect(interruptedAgentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ threadId: 'thread-hitl' }),
      expect.objectContaining({ threadId: 'thread-hitl' }),
    );
  });

  it('stores HITL clarification state and resumes on the next message', async () => {
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-hitl',
        response: 'Which task should I update?',
        interrupt: { question: 'Which task should I update?' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-hitl',
        response: 'Updated the dentist task.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient);

    await expect(
      service.processTextMessage('update my task', 42, { chatId: 100, messageId: 10 }),
    ).resolves.toHaveProperty('response', 'Which task should I update?');
    await expect(
      service.processTextMessage(' \n the dentist task \t ', 42, {
        chatId: 100,
        messageId: 11,
        telegramUsername: 'tester',
        telegramFirstName: 'Test',
      }),
    ).resolves.toHaveProperty('response', 'Updated the dentist task.');

    expect(agentClient.resume).toHaveBeenCalledWith(
      {
        message: 'the dentist task',
        userId: 'telegram:42',
        source: 'telegram',
        telegramIdentity: {
          telegramId: 42,
          username: 'tester',
        },
        requestId: undefined,
        threadId: 'thread-hitl',
      },
      {
        chatId: 100,
        messageId: 11,
        telegramUsername: 'tester',
        telegramFirstName: 'Test',
        threadId: 'thread-hitl',
      },
    );
  });

  it('does not inject reply context into a HITL clarification answer', async () => {
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-hitl',
        response: 'Which task should I update?',
        interrupt: { type: 'clarify', question: 'Which task should I update?' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-hitl',
        response: 'Updated.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient);

    await service.processTextMessage('update my task', 42, { chatId: 100, messageId: 10 });
    await service.processTextMessage(
      ' \n the dentist task \t ',
      42,
      { chatId: 100, messageId: 11 },
      undefined,
      { replyContext: '[In reply to your earlier message: "Which task should I update?"]' },
    );

    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'the dentist task', threadId: 'thread-hitl' }),
      expect.objectContaining({ threadId: 'thread-hitl' }),
    );
  });

  it('resumes pending clarifications across processor instances when they share stores', async () => {
    const store = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const firstAgentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-hitl',
        response: 'Which task should I update?',
        interrupt: { question: 'Which task should I update?' },
        toolResults: [],
      }),
      resume: jest.fn(),
    };
    const secondAgentClient = {
      invoke: jest.fn(),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-hitl',
        response: 'Updated the dentist task.',
        toolResults: [],
      }),
    };

    await createService(firstAgentClient, store, gateStore).processTextMessage('update my task', 42, {
      chatId: 100,
      messageId: 10,
    });
    await expect(
      createService(secondAgentClient, store, gateStore).processTextMessage('the dentist task', 42, {
        chatId: 100,
        messageId: 11,
      }),
    ).resolves.toHaveProperty('response', 'Updated the dentist task.');

    expect(secondAgentClient.invoke).not.toHaveBeenCalled();
    expect(secondAgentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ threadId: 'thread-hitl' }),
      expect.objectContaining({ threadId: 'thread-hitl' }),
    );
  });

  it('keeps pending clarifications separate for different chats from the same user', async () => {
    const agentClient = {
      invoke: jest
        .fn()
        .mockResolvedValueOnce({
          status: 'interrupted',
          threadId: 'thread-chat-a',
          response: 'Which task should I update?',
          interrupt: { question: 'Which task should I update?' },
          toolResults: [],
        })
        .mockResolvedValueOnce({
          status: 'completed',
          threadId: 'thread-chat-b',
          response: 'Started a separate request.',
          toolResults: [],
        }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-chat-a',
        response: 'Updated the dentist task.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient);

    await service.processTextMessage('update my task', 42, { chatId: 100, messageId: 10 });
    await expect(
      service.processTextMessage('show today', 42, { chatId: 200, messageId: 10 }),
    ).resolves.toHaveProperty('response', 'Started a separate request.');
    await expect(
      service.processTextMessage('the dentist task', 42, { chatId: 100, messageId: 11 }),
    ).resolves.toHaveProperty('response', 'Updated the dentist task.');

    expect(agentClient.invoke).toHaveBeenCalledTimes(2);
    expect(agentClient.resume).toHaveBeenCalledTimes(1);
    expect(agentClient.resume.mock.calls[0][0].threadId).toBe('thread-chat-a');
  });

  it('returns the friendly failure response from the agent client', async () => {
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'failed',
        threadId: '',
        response: 'Jarvis is temporarily unavailable. Please try again in a moment.',
        toolResults: [],
        error: 'connection refused',
      }),
      resume: jest.fn(),
    };
    const service = createService(agentClient);

    await expect(service.processTextMessage('hello', 42)).resolves.toHaveProperty(
      'response',
      'Jarvis is temporarily unavailable. Please try again in a moment.',
    );
  });

  it('clears pending clarification state after a failed resume', async () => {
    const agentClient = {
      invoke: jest
        .fn()
        .mockResolvedValueOnce({
          status: 'interrupted',
          threadId: 'thread-hitl',
          response: 'Which task should I update?',
          interrupt: { question: 'Which task should I update?' },
          toolResults: [],
        })
        .mockResolvedValueOnce({
          status: 'completed',
          threadId: 'thread-new',
          response: 'Started a new request.',
          toolResults: [],
        }),
      resume: jest.fn().mockResolvedValue({
        status: 'failed',
        threadId: 'thread-hitl',
        response: 'Jarvis is temporarily unavailable. Please try again in a moment.',
        toolResults: [],
        error: 'connection refused',
      }),
    };
    const service = createService(agentClient);

    await service.processTextMessage('update my task', 42);
    await service.processTextMessage('the dentist task', 42);
    await expect(service.processTextMessage('show today', 42)).resolves.toHaveProperty('response', 'Started a new request.');

    expect(agentClient.resume).toHaveBeenCalledTimes(1);
    expect(agentClient.invoke).toHaveBeenCalledTimes(2);
  });

  it('blocks unrelated text when a confirm interrupt is pending and returns a warning', async () => {
    const store = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-confirm',
        response: '⚠️ Confirm: Delete 5 tasks — this is irreversible.',
        interrupt: { type: 'confirm', summary: 'Delete 5 tasks' },
        toolResults: [],
      }),
      resume: jest.fn(),
    };
    const service = createService(agentClient, store, gateStore);

    const firstResult = await service.processTextMessage('remove all tasks on tuesday', 42, {
      chatId: 100,
      messageId: 10,
    });
    expect(firstResult.interruptType).toBe('confirm');

    const secondResult = await service.processTextMessage('add buy milk', 42, {
      chatId: 100,
      messageId: 11,
    });
    expect(secondResult.response).toMatch(/pending approval/i);
    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(agentClient.invoke).toHaveBeenCalledTimes(1);

    // Confirm a third message with a decision token still resumes the original thread
    agentClient.resume.mockResolvedValue({
      status: 'completed',
      threadId: 'thread-confirm',
      response: 'Action declined — no changes were made.',
      toolResults: [],
    });
    const thirdResult = await createService(agentClient, store, gateStore).processTextMessage('no', 42, {
      chatId: 100,
      messageId: 12,
    });
    expect(thirdResult.response).toBe('Action declined — no changes were made.');
    expect(agentClient.resume).toHaveBeenCalledTimes(1);
  });

  it('resumes a confirm interrupt when user types an approve token', async () => {
    const store = new MemoryPendingClarificationStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-confirm',
        response: '⚠️ Confirm: Delete 5 tasks',
        interrupt: { type: 'confirm', summary: 'Delete 5 tasks' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-confirm',
        response: 'Done. Deleted 5 tasks.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient, store);

    await service.processTextMessage('remove all tasks on tuesday', 42, { chatId: 100, messageId: 10 });
    const result = await service.processTextMessage('yes', 42, { chatId: 100, messageId: 11 });

    expect(result.response).toBe('Done. Deleted 5 tasks.');
    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'yes', threadId: 'thread-confirm' }),
      expect.objectContaining({ threadId: 'thread-confirm' }),
    );
  });

  it('resumes a confirm interrupt when user types a decline token', async () => {
    const store = new MemoryPendingClarificationStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-confirm',
        response: '⚠️ Confirm: Delete 5 tasks',
        interrupt: { type: 'confirm', summary: 'Delete 5 tasks' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-confirm',
        response: 'Action declined — no changes were made.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient, store);

    await service.processTextMessage('remove all tasks on tuesday', 42, { chatId: 100, messageId: 10 });
    const result = await service.processTextMessage('no', 42, { chatId: 100, messageId: 11 });

    expect(result.response).toBe('Action declined — no changes were made.');
    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'no', threadId: 'thread-confirm' }),
      expect.any(Object),
    );
  });

  it('allows any text to resume a clarify interrupt (unchanged behaviour)', async () => {
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-clarify',
        response: 'Which day did you mean?',
        interrupt: { type: 'clarify', question: 'Which day did you mean?' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-clarify',
        response: 'Got it, using Tuesday.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient);

    await service.processTextMessage('remove tasks', 42, { chatId: 100, messageId: 10 });
    const result = await service.processTextMessage('add buy milk', 42, { chatId: 100, messageId: 11 });

    expect(result.response).toBe('Got it, using Tuesday.');
    expect(agentClient.resume).toHaveBeenCalledTimes(1);
  });

  it('starts a new invoke when a pending clarification has expired', async () => {
    process.env.TELEGRAM_PENDING_TTL_MS = '1';
    const agentClient = {
      invoke: jest
        .fn()
        .mockResolvedValueOnce({
          status: 'interrupted',
          threadId: 'thread-hitl',
          response: 'Which task should I update?',
          interrupt: { question: 'Which task should I update?' },
          toolResults: [],
        })
        .mockResolvedValueOnce({
          status: 'completed',
          threadId: 'thread-new',
          response: 'Started a new request.',
          toolResults: [],
        }),
      resume: jest.fn(),
    };
    const service = createService(agentClient);

    await service.processTextMessage('update my task', 42, { chatId: 100, messageId: 10 });
    await new Promise((resolve) => setTimeout(resolve, 5));
    await expect(
      service.processTextMessage('the dentist task', 42, { chatId: 100, messageId: 11 }),
    ).resolves.toHaveProperty('response', 'Started a new request.');

    expect(agentClient.resume).not.toHaveBeenCalled();
    expect(agentClient.invoke).toHaveBeenCalledTimes(2);
  });

  it('appends buffered message to response when gate has a buffered message', async () => {
    const gateStore = new MemoryConversationGateStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Task created: buy milk.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient, undefined, gateStore);

    // Pre-acquire the gate and set a buffered message to simulate a queued message
    const { buildConversationKey } = require('../../../../../src/services/telegram/conversation-key');
    const gateKey = buildConversationKey(42, 'telegram:42', 100);
    await gateStore.tryAcquire(gateKey, 60000);
    await gateStore.setBufferedMessage(gateKey, 'also buy eggs');

    const result = await service.processTextMessage('buy milk', 42, { chatId: 100, messageId: 10 }, undefined, { gatePreAcquired: true });

    expect(result.response).toContain('Task created: buy milk.');
    expect(result.response).toContain('also buy eggs');
    expect(result.response).toContain('You also sent:');
    expect(result.bufferedMessage).toBe('also buy eggs');
  });

  it('does not append buffered suffix when no message was buffered', async () => {
    const gateStore = new MemoryConversationGateStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Task created.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient, undefined, gateStore);

    const { buildConversationKey } = require('../../../../../src/services/telegram/conversation-key');
    const gateKey = buildConversationKey(42, 'telegram:42', 100);
    await gateStore.tryAcquire(gateKey, 60000);

    const result = await service.processTextMessage('buy milk', 42, { chatId: 100, messageId: 10 }, undefined, { gatePreAcquired: true });

    expect(result.response).toBe('Task created.');
    expect(result.bufferedMessage).toBeUndefined();
  });

  it('preserves pending record when resume throws during clarification handling', async () => {
    const store = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-clarify',
        response: 'Which task?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
      resume: jest.fn().mockImplementation(() => Promise.reject(new Error('network timeout'))),
    };
    const service = createService(agentClient, store, gateStore);

    await service.processTextMessage('update task', 42, { chatId: 100, messageId: 10 });

    const { buildConversationKey } = require('../../../../../src/services/telegram/conversation-key');
    const gateKey = buildConversationKey(42, 'telegram:42', 100);

    const pending = await store.get(gateKey);
    expect(pending).not.toBeNull();
    expect(pending!.threadId).toBe('thread-clarify');

    const resultPromise = service.processTextMessage('the dentist task', 42, { chatId: 100, messageId: 11 });
    const result = await resultPromise;

    expect(result.response).toMatch(/went wrong|error|unavailable/i);
    const pendingAfter = await store.get(gateKey);
    expect(pendingAfter).not.toBeNull();
    expect(pendingAfter!.threadId).toBe('thread-clarify');
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
  });

  it('resumes a pre-reserved pending clarification without invoking a fresh thread', async () => {
    const store = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-clarify',
        response: 'Which task?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'completed',
        threadId: 'thread-clarify',
        response: 'Updated it.',
        toolResults: [],
      }),
    };
    const service = createService(agentClient, store, gateStore);

    await service.processTextMessage('update task', 42, { chatId: 100, messageId: 10 });
    const gateKey = buildConversationKey(42, 'telegram:42', 100);
    await gateStore.transitionToRunning(gateKey, 60000);

    const result = await service.processTextMessage(
      'the dentist task',
      42,
      { chatId: 100, messageId: 11 },
      undefined,
      { pendingClarificationPreReserved: true },
    );

    expect(result.response).toBe('Updated it.');
    expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    expect(agentClient.resume).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'the dentist task', threadId: 'thread-clarify' }),
      expect.objectContaining({ threadId: 'thread-clarify' }),
    );
    expect(await store.get(gateKey)).toBeUndefined();
    expect(await gateStore.getStatus(gateKey)).toBe('idle');
  });

  it('stores a new pending record when a pre-reserved clarification resume interrupts again', async () => {
    const store = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-clarify',
        response: 'Which task?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
      resume: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-second',
        response: 'What time?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
    };
    const service = createService(agentClient, store, gateStore);

    await service.processTextMessage('update task', 42, { chatId: 100, messageId: 10 });
    const gateKey = buildConversationKey(42, 'telegram:42', 100);
    await gateStore.transitionToRunning(gateKey, 60000);

    const result = await service.processTextMessage(
      'the dentist task',
      42,
      { chatId: 100, messageId: 11 },
      undefined,
      { pendingClarificationPreReserved: true },
    );

    expect(result.response).toBe('What time?');
    expect(result.interruptType).toBe('clarify');
    const pending = await store.get(gateKey);
    expect(pending?.threadId).toBe('thread-second');
    expect(pending?.question).toBe('What time?');
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
  });

  it('preserves pending state when pre-reserved clarification resume fails', async () => {
    const store = new MemoryPendingClarificationStore();
    const gateStore = new MemoryConversationGateStore();
    const agentClient = {
      invoke: jest.fn().mockResolvedValue({
        status: 'interrupted',
        threadId: 'thread-clarify',
        response: 'Which task?',
        interrupt: { type: 'clarify' },
        toolResults: [],
      }),
      resume: jest.fn().mockRejectedValue(new Error('network timeout')),
    };
    const service = createService(agentClient, store, gateStore);

    await service.processTextMessage('update task', 42, { chatId: 100, messageId: 10 });
    const gateKey = buildConversationKey(42, 'telegram:42', 100);
    await gateStore.transitionToRunning(gateKey, 60000);

    const result = await service.processTextMessage(
      'the dentist task',
      42,
      { chatId: 100, messageId: 11 },
      undefined,
      { pendingClarificationPreReserved: true },
    );

    expect(result.response).toMatch(/Something went wrong/i);
    const pending = await store.get(gateKey);
    expect(pending?.threadId).toBe('thread-clarify');
    expect(await gateStore.getStatus(gateKey)).toBe('waiting_for_clarification');
  });

  describe('consumed clarification presentation', () => {
    // Seeds a pending record and puts the gate into the waiting state so the next message resumes it.
    async function seedPending(
      store: MemoryPendingClarificationStore,
      gateStore: MemoryConversationGateStore,
      interruptType: 'clarify' | 'confirm',
      clarificationMessageId = 321,
    ) {
      const gateKey = buildConversationKey(42, 'telegram:42', 100);
      await gateStore.tryAcquire(gateKey, 60000);
      await gateStore.transitionToWaiting(gateKey, 60000);
      const now = Date.now();
      await store.save({
        pendingKey: gateKey,
        threadId: 'thread-hitl',
        question: 'Which task?',
        telegramUserId: 42,
        chatId: 100,
        userId: 'telegram:42',
        interruptType,
        clarificationMessageId: interruptType === 'clarify' ? clarificationMessageId : undefined,
        status: 'pending',
        createdAt: now,
        updatedAt: now,
        expiresAt: now + 30 * 60 * 1000,
      });
      return gateKey;
    }

    it('surfaces the clarification id when a clarify reply resolves the pause', async () => {
      const store = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      await seedPending(store, gateStore, 'clarify');
      const agentClient = {
        invoke: jest.fn(),
        resume: jest.fn().mockResolvedValue({ status: 'completed', threadId: 'thread-hitl', response: 'Updated.', toolResults: [] }),
      };
      const service = createService(agentClient, store, gateStore);

      const result = await service.processTextMessage('the dentist task', 42, { chatId: 100, messageId: 11 });

      expect(result.consumedClarificationMessageId).toBe(321);
      // The flag confirms that the pending pause—not unrelated result data—was consumed.
      expect(result.resolvedPendingPause).toBe(true);
    });

    it('notifies acceptance after winning the gate and before resuming the agent', async () => {
      const store = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      await seedPending(store, gateStore, 'clarify');
      const onPendingPauseAccepted = jest.fn().mockResolvedValue(undefined);
      const agentClient = {
        invoke: jest.fn(),
        resume: jest.fn().mockResolvedValue({
          status: 'completed',
          threadId: 'thread-hitl',
          response: 'Updated.',
          toolResults: [],
        }),
      };
      const service = createService(agentClient, store, gateStore);

      const result = await service.processTextMessage(
        'the dentist task',
        42,
        { chatId: 100, messageId: 11 },
        undefined,
        { onPendingPauseAccepted },
      );

      expect(onPendingPauseAccepted).toHaveBeenCalledWith({
        clarificationMessageId: 321,
        question: 'Which task?',
      });
      expect(onPendingPauseAccepted.mock.invocationCallOrder[0]).toBeLessThan(
        agentClient.resume.mock.invocationCallOrder[0],
      );
      expect(result.consumedClarificationMessageId).toBeUndefined();
      expect(result.resolvedPendingPause).toBe(true);
    });

    it('does not notify acceptance for invalid typed confirmation text', async () => {
      const store = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      await seedPending(store, gateStore, 'confirm');
      const onPendingPauseAccepted = jest.fn();
      const agentClient = { invoke: jest.fn(), resume: jest.fn() };
      const service = createService(agentClient, store, gateStore);

      await service.processTextMessage(
        'maybe later',
        42,
        { chatId: 100, messageId: 11 },
        undefined,
        { onPendingPauseAccepted },
      );

      expect(onPendingPauseAccepted).not.toHaveBeenCalled();
      expect(agentClient.resume).not.toHaveBeenCalled();
    });

    it('resolves a typed confirmation without clarification presentation data', async () => {
      const store = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      await seedPending(store, gateStore, 'confirm');
      const agentClient = {
        invoke: jest.fn(),
        resume: jest.fn().mockResolvedValue({ status: 'completed', threadId: 'thread-hitl', response: 'Deleted.', toolResults: [] }),
      };
      const service = createService(agentClient, store, gateStore);

      const result = await service.processTextMessage('yes', 42, { chatId: 100, messageId: 11 });

      expect(result.consumedClarificationMessageId).toBeUndefined();
      expect(result.resolvedPendingPause).toBe(true);
    });

    it('does NOT surface the id when a non-decision is refused on a confirm pause', async () => {
      const store = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      await seedPending(store, gateStore, 'confirm');
      const agentClient = { invoke: jest.fn(), resume: jest.fn() };
      const service = createService(agentClient, store, gateStore);

      const result = await service.processTextMessage('maybe later', 42, { chatId: 100, messageId: 11 });

      expect(result.consumedClarificationMessageId).toBeUndefined();
      expect(result.resolvedPendingPause).toBeFalsy();
      expect(agentClient.resume).not.toHaveBeenCalled();
    });

    it('surfaces the superseded clarification id on /new (forceFresh)', async () => {
      const store = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      await seedPending(store, gateStore, 'clarify', 654);
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({ status: 'completed', threadId: 'thread-fresh', response: 'Fresh.', toolResults: [] }),
        resume: jest.fn(),
      };
      const service = createService(agentClient, store, gateStore);

      const result = await service.processTextMessage('start over', 42, { chatId: 100, messageId: 12 }, undefined, { forceFresh: true });

      expect(result.consumedClarificationMessageId).toBe(654);
      expect(result.resolvedPendingPause).toBe(true);
      expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    });

    it('does NOT flag a resolved pause on a fresh message with nothing pending', async () => {
      const store = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({ status: 'completed', threadId: 'thread-new', response: 'Ok.', toolResults: [] }),
        resume: jest.fn(),
      };
      const service = createService(agentClient, store, gateStore);

      // forceFresh with no waiting pause → 'idle' abandon outcome → nothing was superseded.
      const result = await service.processTextMessage('do a thing', 42, { chatId: 100, messageId: 13 }, undefined, { forceFresh: true });

      expect(result.resolvedPendingPause).toBeFalsy();
      expect(result.consumedClarificationMessageId).toBeUndefined();
    });
  });
});
