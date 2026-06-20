import crypto from 'crypto';
import { TextProcessorService } from '../../../../../src/services/telegram/processors/text-processor.service';

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

  afterEach(() => {
    if (originalTelegramUserMap === undefined) {
      delete process.env.TELEGRAM_USER_MAP;
    } else {
      process.env.TELEGRAM_USER_MAP = originalTelegramUserMap;
    }
    jest.restoreAllMocks();
  });

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
    const service = new TextProcessorService(agentClient as any);

    await expect(
      service.processTextMessage('add milk', 701122767, {
        requestId: 'tg_test',
        chatId: 555,
        messageId: 42,
      }),
    ).resolves.toBe('Done.');

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
    const service = new TextProcessorService(agentClient as any);
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
    const service = new TextProcessorService(agentClient as any);

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
    const service = new TextProcessorService(agentClient as any);

    await expect(
      service.processTextMessage('update my task', 42, { chatId: 100, messageId: 10 }),
    ).resolves.toBe('Which task should I update?');
    await expect(
      service.processTextMessage('the dentist task', 42, { chatId: 100, messageId: 11 }),
    ).resolves.toBe('Updated the dentist task.');

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
    const service = new TextProcessorService(agentClient as any);

    await service.processTextMessage('update my task', 42, { chatId: 100, messageId: 10 });
    await expect(
      service.processTextMessage('show today', 42, { chatId: 200, messageId: 10 }),
    ).resolves.toBe('Started a separate request.');
    await expect(
      service.processTextMessage('the dentist task', 42, { chatId: 100, messageId: 11 }),
    ).resolves.toBe('Updated the dentist task.');

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
    const service = new TextProcessorService(agentClient as any);

    await expect(service.processTextMessage('hello', 42)).resolves.toBe(
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
    const service = new TextProcessorService(agentClient as any);

    await service.processTextMessage('update my task', 42);
    await service.processTextMessage('the dentist task', 42);
    await expect(service.processTextMessage('show today', 42)).resolves.toBe('Started a new request.');

    expect(agentClient.resume).toHaveBeenCalledTimes(1);
    expect(agentClient.invoke).toHaveBeenCalledTimes(2);
  });
});
