import { LangGraphAgentClient } from '../../../../src/services/ai/langgraph-agent-client.service';
import { logger } from '../../../../src/utils/logger';

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

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    'rejects an invalid overall timeout: %s',
    (timeoutMs) => {
      expect(
        () => new LangGraphAgentClient({ baseUrl: 'http://localhost:8000', timeoutMs }),
      ).toThrow('timeoutMs must be finite and greater than zero');
    },
  );

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    'rejects an invalid stream idle timeout: %s',
    (streamIdleTimeoutMs) => {
      expect(
        () =>
          new LangGraphAgentClient({
            baseUrl: 'http://localhost:8000',
            timeoutMs: 1000,
            streamIdleTimeoutMs,
          }),
      ).toThrow('streamIdleTimeoutMs must be finite and greater than zero');
    },
  );

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
      delivery: 'terminal',
      threadId: 'thread-1',
      response: 'Done.',
      interrupt: undefined,
      toolResults: [{ tool_name: 'add_todoist_task' }],
      error: undefined,
      errorDetails: undefined,
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

  it.each(['cancelled', 'mutation_in_flight', 'already_finished', 'not_found'] as const)(
    'posts a cancellation request and accepts the %s outcome',
    async (outcome) => {
      const fetchMock = jest.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: jest.fn().mockResolvedValue({ outcome, request_id: 'request-1' }),
      });
      global.fetch = fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000/',
        apiKey: 'secret',
      });

      await expect(client.cancelRun('telegram:123', 'request-1')).resolves.toBe(outcome);
      expect(fetchMock).toHaveBeenCalledWith(
        'http://localhost:8000/runs/cancel',
        expect.objectContaining({
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Jarvis-Agent-Key': 'secret',
          },
          body: JSON.stringify({ user_id: 'telegram:123', request_id: 'request-1' }),
          signal: expect.any(AbortSignal),
        }),
      );
    },
  );

  it('rejects an unknown cancellation outcome', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: jest.fn().mockResolvedValue({ outcome: 'unsafe_to_cancel' }),
    }) as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(client.cancelRun('telegram:123', 'request-1')).rejects.toThrow(
      'LangGraph cancel returned an invalid outcome',
    );
  });

  it('aborts a cancellation request after five seconds', async () => {
    jest.useFakeTimers();
    try {
      global.fetch = jest.fn().mockImplementation((_url, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted', 'AbortError'));
          }, { once: true });
        })) as any;
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const cancellation = client.cancelRun('telegram:123', 'request-1');
      const rejection = expect(cancellation).rejects.toMatchObject({ name: 'AbortError' });
      await jest.advanceTimersByTimeAsync(5_000);
      await rejection;
    } finally {
      jest.useRealTimers();
    }
  });

  it('normalizes network failures into a friendly failed response', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('connection refused')) as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke({ message: 'hello', userId: 'local-user', threadId: 'thread-1' }),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        threadId: 'thread-1',
        response: expect.stringContaining('keeping it active to prevent a duplicate'),
        error: 'connection refused',
      }),
    );
  });

  it('classifies a completed standard 429 response as a terminal rejection', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: false,
      status: 429,
      text: jest.fn().mockResolvedValue('rate limited'),
    });
    global.fetch = fetchMock as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    const result = await client.invoke({ message: 'hello', userId: 'local-user' });

    expect(result).toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'terminal',
        error: 'LangGraph API returned 429',
      }),
    );
    expect(result.response).not.toContain('keeping it active');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps a standard 409 response delivery-ambiguous', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 409,
      text: jest.fn().mockResolvedValue('request is already in progress'),
    }) as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke({ message: 'hello', userId: 'local-user' }),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        error: 'LangGraph API returned 409',
      }),
    );
  });

  it('keeps a standard 429 response ambiguous when its body cannot be read', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 429,
      text: jest.fn().mockRejectedValue(new Error('socket reset while reading rejection')),
    }) as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke({ message: 'hello', userId: 'local-user' }),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        error: 'socket reset while reading rejection',
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
    expect(onProgress).toHaveBeenCalledWith(
      {
        sequence: 1,
        stage: 'run_started',
        message: 'Agent started and opened a Jarvis run',
      },
      expect.any(Object),
    );
  });

  it('logs the backend error returned by a failed stream response', async () => {
    const infoSpy = jest.spyOn(logger, 'info').mockImplementation();
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      body: streamBody([
        {
          type: 'final',
          response: {
            status: 'failed',
            thread_id: '',
            response: 'Jarvis is temporarily unavailable. Please try again in a moment.',
            tool_results: [],
            error: 'Stored user preferences failed schema validation.',
            error_details: {
              source: 'deepseek',
              type: 'timeout',
              retryable: true,
              attempts: 3,
              timeout_kind: 'read',
              request_timeout_seconds: 90,
              total_elapsed_ms: 275000,
              provider_request_id: 'req_deepseek_123',
            },
          },
        },
      ]),
    }) as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    const response = await client.invoke({ message: 'hello', userId: 'local-user' }, {}, jest.fn());

    expect(response.delivery).toBe('terminal');

    expect(infoSpy).toHaveBeenCalledWith(
      'langgraph.stream.completed',
      expect.objectContaining({
        status: 'failed',
        agentError: 'Stored user preferences failed schema validation.',
        backendErrorSource: 'deepseek',
        backendErrorType: 'timeout',
        backendErrorRetryable: true,
        backendErrorAttempts: 3,
        backendErrorTimeoutKind: 'read',
        backendErrorRequestTimeoutSeconds: 90,
        backendErrorTotalElapsedMs: 275000,
        backendErrorProviderRequestId: 'req_deepseek_123',
      }),
    );
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
      client.invoke(
        { message: 'hello', userId: 'local-user', requestId: 'request-fallback' },
        {},
        jest.fn(),
      ),
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

  it('does not fall back after an ambiguous stream setup failure without a request id', async () => {
    const fetchMock = jest.fn().mockRejectedValue(new Error('stream unavailable'));
    global.fetch = fetchMock as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke({ message: 'hello', userId: 'local-user' }, {}, jest.fn()),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        error: 'stream unavailable',
      }),
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does not retry or fall back after a pre-header 5xx without a request id', async () => {
    const fetchMock = jest.fn().mockResolvedValue({ ok: false, status: 503 });
    global.fetch = fetchMock as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    const result = await client.invoke(
      { message: 'hello', userId: 'local-user' },
      {},
      jest.fn(),
    );

    expect(result).toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        error: 'LangGraph API returned 503',
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('classifies a completed stream 429 response as a terminal rejection', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: false,
      status: 429,
      text: jest.fn().mockResolvedValue('rate limited'),
    });
    global.fetch = fetchMock as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    const result = await client.invoke(
      { message: 'hello', userId: 'local-user', requestId: 'rate-limited' },
      {},
      jest.fn(),
    );

    expect(result).toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'terminal',
        error: 'LangGraph stream returned 429',
      }),
    );
    expect(result.response).not.toContain('keeping it active');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps a stream 409 response delivery-ambiguous', async () => {
    const fetchMock = jest.fn().mockResolvedValue({
      ok: false,
      status: 409,
      text: jest.fn().mockResolvedValue('request is already in progress'),
    });
    global.fetch = fetchMock as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    const result = await client.invoke(
      { message: 'hello', userId: 'local-user', requestId: 'conflict' },
      {},
      jest.fn(),
    );

    expect(result).toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        error: 'LangGraph stream returned 409',
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps a stream 429 response ambiguous when its body cannot be read', async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 429,
      text: jest.fn().mockRejectedValue(new Error('socket reset while reading rejection')),
    }) as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    await expect(
      client.invoke(
        { message: 'hello', userId: 'local-user', requestId: 'rate-limited-read-error' },
        {},
        jest.fn(),
      ),
    ).resolves.toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        error: 'socket reset while reading rejection',
      }),
    );
  });

  it('does not retry or fall back after stream response headers arrive with a 5xx', async () => {
    const warnSpy = jest.spyOn(logger, 'warn').mockImplementation();
    const fetchMock = jest.fn().mockResolvedValue({ ok: false, status: 503 });
    global.fetch = fetchMock as any;
    const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

    const result = await client.invoke(
      { message: 'hello', userId: 'local-user', requestId: 'request-with-id' },
      {},
      jest.fn(),
    );

    expect(result).toEqual(
      expect.objectContaining({
        status: 'failed',
        delivery: 'ambiguous',
        error: 'LangGraph API returned 503',
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(warnSpy).toHaveBeenCalledWith(
      'langgraph.stream.failed',
      expect.objectContaining({
        failureKind: 'http_error',
        responseReceived: true,
        streamStarted: false,
      }),
    );
  });

  describe('standard response deadlines and retries', () => {
    beforeEach(() => {
      jest.useFakeTimers();
    });

    afterEach(() => {
      jest.useRealTimers();
    });

    it('aborts a stalled standard response body after headers', async () => {
      let capturedSignal: AbortSignal | undefined;
      const fetchMock = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
        capturedSignal = init.signal ?? undefined;
        return Promise.resolve({
          ok: true,
          text: () => new Promise<string>(() => {}),
        });
      });
      global.fetch = fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 1000,
      });

      const promise = client.invoke({ message: 'hi', userId: 'u', threadId: 't' });
      await jest.advanceTimersByTimeAsync(1000);
      const result = await promise;

      expect(result.status).toBe('failed');
      expect(result.response).toContain('finish this in time');
      expect(capturedSignal?.aborted).toBe(true);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it('applies the body deadline to a non-streaming fallback response', async () => {
      let fallbackSignal: AbortSignal | undefined;
      const fetchMock = jest
        .fn()
        .mockRejectedValueOnce(new Error('stream unavailable'))
        .mockImplementationOnce((_url: string, init: RequestInit) => {
          fallbackSignal = init.signal ?? undefined;
          return Promise.resolve({
            ok: true,
            text: () => new Promise<string>(() => {}),
          });
        });
      global.fetch = fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 1000,
      });

      const promise = client.invoke(
        { message: 'hi', userId: 'u', requestId: 'fallback-timeout', threadId: 't' },
        {},
        jest.fn(),
      );
      await jest.advanceTimersByTimeAsync(1);
      await jest.advanceTimersByTimeAsync(1000);
      const result = await promise;

      expect(result.status).toBe('failed');
      expect(result.response).toContain('finish this in time');
      expect(fallbackSignal?.aborted).toBe(true);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('cancels a retryable 5xx body before issuing the next POST', async () => {
      const cancel = jest.fn().mockResolvedValue(undefined);
      const fetchMock = jest
        .fn()
        .mockResolvedValueOnce({
          ok: false,
          status: 503,
          body: { cancel },
        })
        .mockImplementationOnce(() => {
          expect(cancel).toHaveBeenCalledTimes(1);
          return Promise.resolve({
            ok: true,
            text: jest.fn().mockResolvedValue(
              JSON.stringify({
                status: 'completed',
                thread_id: 'thread-retried',
                response: 'Done after retry.',
              }),
            ),
          });
        });
      global.fetch = fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 10000,
      });

      const promise = client.invoke({
        message: 'hi',
        userId: 'u',
        requestId: 'retry-request',
      });
      await jest.advanceTimersByTimeAsync(1000);

      await expect(promise).resolves.toEqual(
        expect.objectContaining({
          status: 'completed',
          threadId: 'thread-retried',
          response: 'Done after retry.',
        }),
      );
      expect(cancel).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
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
      expect(onProgress).toHaveBeenCalledWith(
        {
          sequence: 1,
          stage: 'thinking',
          message: 'Working...',
        },
        expect.any(Object),
      );
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
          delivery: 'ambiguous',
          threadId: 'thread-x',
          response: expect.stringContaining('keeping it active to prevent a duplicate'),
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
      expect(onProgress).toHaveBeenNthCalledWith(
        1,
        {
          sequence: 1,
          stage: 'thinking',
          message: 'Step 1',
        },
        expect.any(Object),
      );
      expect(onProgress).toHaveBeenNthCalledWith(
        2,
        {
          sequence: 2,
          stage: 'tool_call',
          message: 'Step 2',
        },
        expect.any(Object),
      );
      expect(onProgress).toHaveBeenNthCalledWith(
        3,
        {
          sequence: 3,
          stage: 'summarizing',
          message: 'Step 3',
        },
        expect.any(Object),
      );
    });
  });

  describe('stream deadlines', () => {
    function abortableStreamFetch() {
      let streamController!: ReadableStreamDefaultController<Uint8Array>;
      let capturedSignal: AbortSignal | undefined;
      let bodyCancelled = false;
      const encoder = new TextEncoder();

      const fetchMock = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
        capturedSignal = init.signal ?? undefined;
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller;
          },
          cancel() {
            bodyCancelled = true;
          },
        });
        capturedSignal?.addEventListener('abort', () => {
          try {
            streamController.error(
              capturedSignal?.reason ?? new DOMException('The operation was aborted', 'AbortError'),
            );
          } catch {
            // The test stream may already be closed or errored.
          }
        });
        return Promise.resolve({ ok: true, body });
      });

      return {
        fetchMock,
        push: (obj: unknown) =>
          streamController.enqueue(encoder.encode(`${JSON.stringify(obj)}\n`)),
        pushRaw: (chunk: string) => streamController.enqueue(encoder.encode(chunk)),
        close: () => streamController.close(),
        fail: (error: Error) => streamController.error(error),
        get signal() {
          return capturedSignal;
        },
        get bodyCancelled() {
          return bodyCancelled;
        },
      };
    }

    function stallingFetch() {
      const fetchMock = jest.fn().mockImplementation((_url: string, init: RequestInit) => {
        return new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(init.signal?.reason ?? new DOMException('The operation was aborted', 'AbortError'));
          });
        });
      });
      return { fetchMock };
    }

    const finalPayload = {
      status: 'completed' as const,
      thread_id: 'thread-1',
      response: 'All done.',
      tool_results: [],
    };

    beforeEach(() => {
      jest.useFakeTimers();
    });

    afterEach(() => {
      jest.useRealTimers();
    });

    it('aborts a pre-header connection stall at the overall deadline without falling back', async () => {
      const { fetchMock } = stallingFetch();
      global.fetch = fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 100000,
        streamIdleTimeoutMs: 90000,
      });

      const promise = client.invoke(
        { message: 'hi', userId: 'u', threadId: 't', requestId: 'deadline-request' },
        {},
        jest.fn(),
      );
      await jest.advanceTimersByTimeAsync(100000);
      const result = await promise;

      expect(result).toEqual(
        expect.objectContaining({
          status: 'failed',
          threadId: 't',
          response: expect.stringContaining('finish this in time'),
        }),
      );
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it('idle-times out after headers and tears down the body read', async () => {
      const warnSpy = jest.spyOn(logger, 'warn').mockImplementation();
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 150000,
        streamIdleTimeoutMs: 90000,
      });

      const onProgress = jest.fn();
      const promise = client.invoke(
        { message: 'hi', userId: 'u', threadId: 't', requestId: 'idle-request' },
        {},
        onProgress,
      );
      await jest.advanceTimersByTimeAsync(1);
      harness.push({ type: 'progress', sequence: 1, stage: 'x', message: 'working' });
      await jest.advanceTimersByTimeAsync(1);
      expect(onProgress).toHaveBeenCalledTimes(1);

      await jest.advanceTimersByTimeAsync(90000);
      const result = await promise;

      expect(result.status).toBe('failed');
      expect(result.response).toContain('finish this in time');
      expect(harness.fetchMock).toHaveBeenCalledTimes(1);
      expect(harness.signal?.aborted).toBe(true);
      expect(warnSpy).toHaveBeenCalledWith(
        'langgraph.stream.failed',
        expect.objectContaining({ failureKind: 'idle_timeout', streamStarted: true }),
      );
    });

    it('resets the idle deadline whenever a chunk arrives', async () => {
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 10_000_000,
        streamIdleTimeoutMs: 90000,
      });

      const onProgress = jest.fn();
      const promise = client.invoke({ message: 'hi', userId: 'u' }, {}, onProgress);
      await jest.advanceTimersByTimeAsync(1);
      harness.push({ type: 'progress', sequence: 1, stage: 'x', message: 'a' });
      await jest.advanceTimersByTimeAsync(80000);
      harness.push({ type: 'progress', sequence: 2, stage: 'x', message: 'b' });
      await jest.advanceTimersByTimeAsync(80000);
      harness.push({ type: 'progress', sequence: 3, stage: 'x', message: 'c' });
      await jest.advanceTimersByTimeAsync(80000);
      harness.push({ type: 'final', response: finalPayload });
      harness.close();
      await jest.advanceTimersByTimeAsync(1);

      await expect(promise).resolves.toEqual(
        expect.objectContaining({ status: 'completed', response: 'All done.' }),
      );
      expect(onProgress).toHaveBeenCalledTimes(3);
    });

    it('enforces the overall deadline even while progress keeps arriving', async () => {
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 100000,
        streamIdleTimeoutMs: 500000,
      });

      const promise = client.invoke(
        { message: 'hi', userId: 'u', threadId: 't', requestId: 'overall-request' },
        {},
        jest.fn(),
      );
      await jest.advanceTimersByTimeAsync(1);
      harness.push({ type: 'progress', sequence: 1, stage: 'x', message: 'a' });
      await jest.advanceTimersByTimeAsync(50000);
      harness.push({ type: 'progress', sequence: 2, stage: 'x', message: 'b' });
      await jest.advanceTimersByTimeAsync(50000);

      const result = await promise;
      expect(result.status).toBe('failed');
      expect(result.delivery).toBe('ambiguous');
      expect(result.response).toContain('finish this in time');
      expect(harness.fetchMock).toHaveBeenCalledTimes(1);
      expect(harness.signal?.aborted).toBe(true);
    });

    it('classifies an errored response body as a stream error without re-posting', async () => {
      const warnSpy = jest.spyOn(logger, 'warn').mockImplementation();
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const promise = client.invoke(
        { message: 'hi', userId: 'u', requestId: 'stream-error-request' },
        {},
        jest.fn(),
      );
      await jest.advanceTimersByTimeAsync(1);
      harness.fail(new Error('socket reset while reading'));
      await jest.advanceTimersByTimeAsync(1);
      const result = await promise;

      expect(result).toEqual(
        expect.objectContaining({
          status: 'failed',
          delivery: 'ambiguous',
          error: 'socket reset while reading',
        }),
      );
      expect(harness.fetchMock).toHaveBeenCalledTimes(1);
      expect(warnSpy).toHaveBeenCalledWith(
        'langgraph.stream.failed',
        expect.objectContaining({ failureKind: 'stream_error', streamStarted: true }),
      );
    });

    it('returns and cancels the body when a final event arrives before EOF', async () => {
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });
      const onProgress = jest.fn();

      const promise = client.invoke({ message: 'hi', userId: 'u' }, {}, onProgress);
      await jest.advanceTimersByTimeAsync(1);
      harness.pushRaw(
        `${JSON.stringify({ type: 'final', response: finalPayload })}\n${JSON.stringify({
          type: 'progress',
          sequence: 2,
          stage: 'late',
          message: 'must be ignored',
        })}\n`,
      );
      await jest.advanceTimersByTimeAsync(1);

      await expect(promise).resolves.toEqual(
        expect.objectContaining({ status: 'completed', response: 'All done.' }),
      );
      expect(harness.bodyCancelled).toBe(true);
      expect(harness.signal?.aborted).toBe(false);
      expect(onProgress).not.toHaveBeenCalled();
    });

    it('ignores a rejected progress callback and preserves the final response', async () => {
      const warnSpy = jest.spyOn(logger, 'warn').mockImplementation();
      global.fetch = jest.fn().mockResolvedValue({
        ok: true,
        body: streamBody([
          { type: 'progress', sequence: 1, stage: 'x', message: 'working' },
          { type: 'final', response: finalPayload },
        ]),
      }) as any;
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });
      const onProgress = jest.fn().mockRejectedValue(new Error('telegram unavailable'));

      await expect(client.invoke({ message: 'hi', userId: 'u' }, {}, onProgress)).resolves.toEqual(
        expect.objectContaining({ status: 'completed', response: 'All done.' }),
      );
      expect(warnSpy).toHaveBeenCalledWith(
        'langgraph.stream.progress_callback_failed',
        expect.objectContaining({ stage: 'x', error: 'telegram unavailable' }),
      );
    });

    it('bounds a hung progress callback and still consumes a queued final response', async () => {
      const warnSpy = jest.spyOn(logger, 'warn').mockImplementation();
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });
      let callbackSignal: AbortSignal | undefined;
      const onProgress = jest.fn((_event: unknown, signal?: AbortSignal) => {
        callbackSignal = signal;
        return new Promise<void>(() => {});
      });

      const promise = client.invoke({ message: 'hi', userId: 'u' }, {}, onProgress);
      await jest.advanceTimersByTimeAsync(1);
      harness.push({ type: 'progress', sequence: 1, stage: 'x', message: 'working' });
      harness.push({ type: 'final', response: finalPayload });
      await jest.advanceTimersByTimeAsync(5000);

      await expect(promise).resolves.toEqual(
        expect.objectContaining({ status: 'completed', response: 'All done.' }),
      );
      expect(warnSpy).toHaveBeenCalledWith(
        'langgraph.stream.progress_callback_timed_out',
        expect.objectContaining({ stage: 'x', timeoutMs: 5000 }),
      );
      expect(callbackSignal?.aborted).toBe(true);
      expect(harness.bodyCancelled).toBe(true);
    });

    it('lets the overall deadline interrupt a hung progress callback', async () => {
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({
        baseUrl: 'http://localhost:8000',
        timeoutMs: 1000,
        streamIdleTimeoutMs: 5000,
      });
      const onProgress = jest.fn().mockReturnValue(new Promise<void>(() => {}));

      const promise = client.invoke({ message: 'hi', userId: 'u' }, {}, onProgress);
      await jest.advanceTimersByTimeAsync(1);
      harness.push({ type: 'progress', sequence: 1, stage: 'x', message: 'working' });
      await jest.advanceTimersByTimeAsync(1000);
      const result = await promise;

      expect(result.status).toBe('failed');
      expect(result.response).toContain('finish this in time');
      expect(harness.signal?.aborted).toBe(true);
    });

    it.each([
      ['overall', 1000, 5000, 1000],
      ['idle', 10000, 1000, 1000],
    ])(
      'lets a fired %s deadline win over a same-chunk buffered final event',
      async (_kind, timeoutMs, streamIdleTimeoutMs, advanceMs) => {
        const harness = abortableStreamFetch();
        global.fetch = harness.fetchMock as any;
        const client = new LangGraphAgentClient({
          baseUrl: 'http://localhost:8000',
          timeoutMs: timeoutMs as number,
          streamIdleTimeoutMs: streamIdleTimeoutMs as number,
        });
        const onProgress = jest.fn().mockReturnValue(new Promise<void>(() => {}));

        const promise = client.invoke(
          { message: 'hi', userId: 'u', requestId: 'buffered-final-request' },
          {},
          onProgress,
        );
        await jest.advanceTimersByTimeAsync(1);
        harness.pushRaw(
          `${JSON.stringify({
            type: 'progress',
            sequence: 1,
            stage: 'x',
            message: 'working',
          })}\n${JSON.stringify({ type: 'final', response: finalPayload })}\n`,
        );
        await jest.advanceTimersByTimeAsync(advanceMs as number);
        const result = await promise;

        expect(result.status).toBe('failed');
        expect(result.response).toContain('finish this in time');
        expect(result.response).not.toBe('All done.');
        expect(harness.signal?.aborted).toBe(true);
      },
    );

    it('clears both deadline timers after a normal completion', async () => {
      const harness = abortableStreamFetch();
      global.fetch = harness.fetchMock as any;
      const client = new LangGraphAgentClient({ baseUrl: 'http://localhost:8000' });

      const promise = client.invoke({ message: 'hi', userId: 'u' }, {}, jest.fn());
      await jest.advanceTimersByTimeAsync(1);
      harness.push({ type: 'final', response: finalPayload });
      harness.close();
      await jest.advanceTimersByTimeAsync(1);

      await expect(promise).resolves.toEqual(expect.objectContaining({ status: 'completed' }));
      expect(harness.signal?.aborted).toBe(false);
      await jest.advanceTimersByTimeAsync(200000);
      expect(harness.signal?.aborted).toBe(false);
    });
  });
});
