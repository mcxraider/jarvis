import { LangGraphAgentClient } from '../../../../src/services/ai/langgraph-agent-client.service';

describe('LangGraphAgentClient', () => {
  const originalFetch = global.fetch;

  afterEach(() => {
    global.fetch = originalFetch;
    jest.restoreAllMocks();
  });

  it('posts invoke requests using the Python API payload shape', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      text: jest.fn().mockResolvedValue(
        JSON.stringify({
          status: 'completed',
          thread_id: 'thread-1',
          response: 'Done.',
          tool_results: [{ tool_name: 'add_todoist_task' }],
        }),
      ),
    });
    global.fetch = fetchMock as any;

    const client = new LangGraphAgentClient({
      baseUrl: 'http://localhost:8000/',
      timeoutMs: 1000,
      apiKey: 'secret',
    });

    await expect(
      client.invoke({
        message: 'add milk',
        userId: 'local-user',
        source: 'test',
        telegramUserId: 123,
        requestId: 'tg_test',
      }),
    ).resolves.toEqual({
      status: 'completed',
      threadId: 'thread-1',
      response: 'Done.',
      interrupt: undefined,
      toolResults: [{ tool_name: 'add_todoist_task' }],
      error: undefined,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:8000/invoke',
      expect.objectContaining({
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Jarvis-Agent-Key': 'secret',
        },
        body: JSON.stringify({
          message: 'add milk',
          user_id: 'local-user',
          source: 'test',
          telegram_user_id: 123,
          request_id: 'tg_test',
          thread_id: undefined,
        }),
      }),
    );
  });

  it('posts resume requests with the existing thread id', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      text: jest.fn().mockResolvedValue(
        JSON.stringify({
          status: 'completed',
          thread_id: 'thread-1',
          response: 'Updated.',
        }),
      ),
    });
    global.fetch = fetchMock as any;

    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });
    const response = await client.resume({
      message: 'the dentist task',
      userId: 'local-user',
      source: 'telegram',
      telegramUserId: 123,
      requestId: 'tg_test',
      threadId: 'thread-1',
    });

    expect(response.response).toBe('Updated.');
    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:8000/resume',
      expect.objectContaining({
        body: JSON.stringify({
          message: 'the dentist task',
          user_id: 'local-user',
          source: 'telegram',
          telegram_user_id: 123,
          request_id: 'tg_test',
          thread_id: 'thread-1',
        }),
      }),
    );
  });

  it('normalizes network failures into a friendly failed response', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('connection refused')) as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke({ message: 'hello', userId: 'local-user', threadId: 'thread-1' }),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'failed',
        threadId: 'thread-1',
        response: 'Jarvis is temporarily unavailable. Please try again in a moment.',
        error: 'connection refused',
      }),
    );
  });
});
