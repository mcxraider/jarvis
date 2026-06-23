import crypto from 'crypto';
import { TextProcessorService } from '../../../../../src/services/telegram/processors/text-processor.service';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';

function telegramThreadId(identity: number | string, messageKey: number | string): string {
  const hash = crypto.createHash('sha256').update(String(identity)).digest('hex').slice(0, 10);
  const segment = String(messageKey)
    .trim()
    .replace(/[^a-zA-Z0-9_-]/g, '_')
    .slice(0, 64);

  return `tg_${hash}_${segment}`;
}

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

  function createService(agentClient: unknown, store = new MemoryPendingClarificationStore()): TextProcessorService {
    return new TextProcessorService(agentClient as any, store);
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
      service.processTextMessage('add milk', 701122767, {
        requestId: 'tg_test',
        chatId: 555,
        messageId: 42,
      }),
    ).resolves.toHaveProperty('response', 'Done.');

    expect(agentClient.invoke).toHaveBeenCalledWith(
      {
        message: 'add milk',
        userId: 'jerry',
        source: 'telegram',
        telegramUserId: 701122767,
        requestId: 'tg_test',
        threadId: telegramThreadId(555, 42),
      },
      {
        requestId: 'tg_test',
        chatId: 555,
        messageId: 42,
        threadId: telegramThreadId(555, 42),
      },
    );
    expect(agentClient.resume).not.toHaveBeenCalled();
  });

  it('builds the same thread id for Telegram webhook retries of the same message', async () => {
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
    const logContext = { requestId: 'tg_retry', chatId: 555, messageId: 42 };

    await service.processTextMessage('add milk', 701122767, logContext);
    await service.processTextMessage('add milk', 701122767, logContext);

    const threadIds = agentClient.invoke.mock.calls.map(([request]) => request.threadId);
    expect(threadIds).toEqual([telegramThreadId(555, 42), telegramThreadId(555, 42)]);
  });

  it('builds different thread ids for different Telegram chats with the same message id', async () => {
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
    await service.processTextMessage('add milk', 701122767, { chatId: 777, messageId: 42 });

    const threadIds = agentClient.invoke.mock.calls.map(([request]) => request.threadId);
    expect(threadIds).toEqual([telegramThreadId(555, 42), telegramThreadId(777, 42)]);
    expect(threadIds[0]).not.toBe(threadIds[1]);
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
      service.processTextMessage('the dentist task', 42, { chatId: 100, messageId: 11 }),
    ).resolves.toHaveProperty('response', 'Updated the dentist task.');

    expect(agentClient.resume).toHaveBeenCalledWith(
      {
        message: 'the dentist task',
        userId: 'telegram:42',
        source: 'telegram',
        telegramUserId: 42,
        requestId: undefined,
        threadId: 'thread-hitl',
      },
      {
        chatId: 100,
        messageId: 11,
        threadId: 'thread-hitl',
      },
    );
  });

  it('resumes pending clarifications across processor instances when they share a store', async () => {
    const store = new MemoryPendingClarificationStore();
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

    await createService(firstAgentClient, store).processTextMessage('update my task', 42, {
      chatId: 100,
      messageId: 10,
    });
    await expect(
      createService(secondAgentClient, store).processTextMessage('the dentist task', 42, {
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
    const service = createService(agentClient, store);

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
    const thirdResult = await createService(agentClient, store).processTextMessage('no', 42, {
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
});
