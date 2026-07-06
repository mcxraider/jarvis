import { LangGraphAgentClient } from '../../../../src/services/ai/langgraph-agent-client.service';

describe('LangGraphAgentClient', () => {
  const originalFetch = global.fetch;

  function streamBody(lines: unknown[]): ReadableStream<Uint8Array> {
    return new ReadableStream<Uint8Array>({
      start(controller) {
        const encoder = new TextEncoder();
        for (const line of lines) {
          controller.enqueue(encoder.encode(`${JSON.stringify(line)}\n`));
        }
        controller.close();
      },
    });
  }

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
        telegramIdentity: {
          telegramId: 123,
          username: 'tester',
        },
        requestId: 'tg_test',
        threadId: 'tg_test_thread',
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
          telegram_identity: {
            telegram_id: 123,
            username: 'tester',
          },
          request_id: 'tg_test',
          thread_id: 'tg_test_thread',
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
      telegramIdentity: {
        telegramId: 123,
        username: 'tester',
      },
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
          telegram_identity: {
            telegram_id: 123,
            username: 'tester',
          },
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

  it('parses streamed progress events and the final invoke response', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: true,
      body: streamBody([
        {
          type: 'progress',
          sequence: 1,
          stage: 'run_started',
          message: 'Agent started and opened a Jarvis run',
        },
        {
          type: 'final',
          response: {
            status: 'completed',
            thread_id: 'thread-1',
            response: 'Done.',
            tool_results: [],
          },
        },
      ]),
    });
    global.fetch = fetchMock as any;
    const onProgress = jest.fn();
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke({ message: 'hello', userId: 'local-user' }, {}, onProgress),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Done.',
      }),
    );

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:8000/invoke/stream',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(onProgress).toHaveBeenCalledWith({
      sequence: 1,
      stage: 'run_started',
      message: 'Agent started and opened a Jarvis run',
    });
  });

  it('falls back to non-streaming invoke when stream setup fails', async () => {
    const fetchMock = jest
      .fn()
      .mockRejectedValueOnce(new Error('stream unavailable'))
      .mockResolvedValueOnce({
        ok: true,
        text: jest.fn().mockResolvedValue(
          JSON.stringify({
            status: 'completed',
            thread_id: 'thread-fallback',
            response: 'Fallback done.',
          }),
        ),
      });
    global.fetch = fetchMock as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke({ message: 'hello', userId: 'local-user' }, {}, jest.fn()),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'completed',
        threadId: 'thread-fallback',
        response: 'Fallback done.',
      }),
    );

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://localhost:8000/invoke/stream',
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(2, 'http://localhost:8000/invoke', expect.any(Object));
  });

  describe('streaming NDJSON edge cases', () => {
    function createMockStream(chunks: string[]): ReadableStream<Uint8Array> {
      const encoder = new TextEncoder();
      let index = 0;
      return new ReadableStream({
        pull(controller) {
          if (index < chunks.length) {
            controller.enqueue(encoder.encode(chunks[index]));
            index++;
          } else {
            controller.close();
          }
        },
      });
    }

    const finalPayload = {
      status: 'completed' as const,
      thread_id: 'thread-1',
      response: 'All done.',
      tool_results: [],
    };

    it('handles a progress event split across two chunks', async () => {
      const fullLine = JSON.stringify({
        type: 'progress',
        sequence: 1,
        stage: 'thinking',
        message: 'Working...',
      });
      // Split the JSON mid-string
      const splitPoint = Math.floor(fullLine.length / 2);
      const chunk1 = fullLine.slice(0, splitPoint);
      const chunk2 = fullLine.slice(splitPoint) + '\n';
      const finalLine = JSON.stringify({ type: 'final', response: finalPayload }) + '\n';

      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        body: createMockStream([chunk1, chunk2, finalLine]),
      });
      global.fetch = fetchMock as any;

      const onProgress = jest.fn();
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const result = await client.invoke(
        { message: 'hello', userId: 'local-user' },
        {},
        onProgress,
      );

      expect(onProgress).toHaveBeenCalledTimes(1);
      expect(onProgress).toHaveBeenCalledWith({
        sequence: 1,
        stage: 'thinking',
        message: 'Working...',
      });
      expect(result.status).toBe('completed');
      expect(result.response).toBe('All done.');
    });

    it('handles final event split at chunk boundary', async () => {
      const finalLine = JSON.stringify({ type: 'final', response: finalPayload });
      // Split at a point before the newline
      const splitPoint = Math.floor(finalLine.length / 2);
      const chunk1 = finalLine.slice(0, splitPoint);
      const chunk2 = finalLine.slice(splitPoint) + '\n';

      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        body: createMockStream([chunk1, chunk2]),
      });
      global.fetch = fetchMock as any;

      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const result = await client.invoke(
        { message: 'hello', userId: 'local-user' },
        {},
        jest.fn(),
      );

      expect(result).toEqual(
        expect.objectContaining({
          status: 'completed',
          threadId: 'thread-1',
          response: 'All done.',
        }),
      );
    });

    it('returns fallback when stream ends without final event', async () => {
      const progressLine =
        JSON.stringify({
          type: 'progress',
          sequence: 1,
          stage: 'thinking',
          message: 'Working...',
        }) + '\n';

      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        body: createMockStream([progressLine]),
      });
      global.fetch = fetchMock as any;

      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const result = await client.invoke(
        { message: 'hello', userId: 'local-user', threadId: 'thread-x' },
        {},
        jest.fn(),
      );

      // Stream started successfully but ended without final event — returns error fallback
      expect(result).toEqual(
        expect.objectContaining({
          status: 'failed',
          threadId: 'thread-x',
          response: 'Jarvis is temporarily unavailable. Please try again in a moment.',
          error: 'LangGraph stream ended without a final response',
        }),
      );
    });

    it('skips malformed JSON lines without crashing', async () => {
      const progressLine =
        JSON.stringify({
          type: 'progress',
          sequence: 1,
          stage: 'thinking',
          message: 'Working...',
        }) + '\n';
      const malformedLine = 'not valid json\n';
      const finalLine = JSON.stringify({ type: 'final', response: finalPayload }) + '\n';

      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        body: createMockStream([progressLine, malformedLine, finalLine]),
      });
      global.fetch = fetchMock as any;

      const onProgress = jest.fn();
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const result = await client.invoke(
        { message: 'hello', userId: 'local-user' },
        {},
        onProgress,
      );

      expect(result).toEqual(
        expect.objectContaining({
          status: 'completed',
          threadId: 'thread-1',
          response: 'All done.',
        }),
      );
      expect(onProgress).toHaveBeenCalledTimes(1);
    });

    it('ignores empty lines between events', async () => {
      const progressLine =
        JSON.stringify({
          type: 'progress',
          sequence: 1,
          stage: 'thinking',
          message: 'Working...',
        }) + '\n';
      const finalLine = JSON.stringify({ type: 'final', response: finalPayload }) + '\n';

      // Deliver events with empty lines interspersed
      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        body: createMockStream([progressLine, '\n\n', finalLine]),
      });
      global.fetch = fetchMock as any;

      const onProgress = jest.fn();
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const result = await client.invoke(
        { message: 'hello', userId: 'local-user' },
        {},
        onProgress,
      );

      expect(result).toEqual(
        expect.objectContaining({
          status: 'completed',
          threadId: 'thread-1',
          response: 'All done.',
        }),
      );
      expect(onProgress).toHaveBeenCalledTimes(1);
    });

    it('handles stream with only a final event', async () => {
      const finalLine = JSON.stringify({ type: 'final', response: finalPayload }) + '\n';

      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        body: createMockStream([finalLine]),
      });
      global.fetch = fetchMock as any;

      const onProgress = jest.fn();
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const result = await client.invoke(
        { message: 'hello', userId: 'local-user' },
        {},
        onProgress,
      );

      expect(result).toEqual(
        expect.objectContaining({
          status: 'completed',
          threadId: 'thread-1',
          response: 'All done.',
        }),
      );
      expect(onProgress).not.toHaveBeenCalled();
    });

    it('calls onProgress for each progress event', async () => {
      const progress1 =
        JSON.stringify({
          type: 'progress',
          sequence: 1,
          stage: 'thinking',
          message: 'Step 1',
        }) + '\n';
      const progress2 =
        JSON.stringify({
          type: 'progress',
          sequence: 2,
          stage: 'tool_call',
          message: 'Step 2',
        }) + '\n';
      const progress3 =
        JSON.stringify({
          type: 'progress',
          sequence: 3,
          stage: 'summarizing',
          message: 'Step 3',
        }) + '\n';
      const finalLine = JSON.stringify({ type: 'final', response: finalPayload }) + '\n';

      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        body: createMockStream([progress1, progress2, progress3, finalLine]),
      });
      global.fetch = fetchMock as any;

      const onProgress = jest.fn();
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      await client.invoke({ message: 'hello', userId: 'local-user' }, {}, onProgress);

      expect(onProgress).toHaveBeenCalledTimes(3);
      expect(onProgress).toHaveBeenNthCalledWith(1, {
        sequence: 1,
        stage: 'thinking',
        message: 'Step 1',
      });
      expect(onProgress).toHaveBeenNthCalledWith(2, {
        sequence: 2,
        stage: 'tool_call',
        message: 'Step 2',
      });
      expect(onProgress).toHaveBeenNthCalledWith(3, {
        sequence: 3,
        stage: 'summarizing',
        message: 'Step 3',
      });
    });
  });
});
