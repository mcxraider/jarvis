import OpenAI from 'openai';
import { FunctionCallingProcessor } from '../../src/services/ai/processors/function-calling.processor';
import { ToolDispatcher } from '../../src/types/tool.types';
import { createTestRunLogger } from '../helpers/test-run-logger';

const logger = createTestRunLogger('integration-function-calling');

function createOpenAI(create: jest.Mock): OpenAI {
  return {
    chat: {
      completions: {
        create,
      },
    },
  } as unknown as OpenAI;
}

function createToolCall(id: string, name: string, parameters: Record<string, unknown>) {
  return {
    id,
    type: 'function',
    function: {
      name,
      arguments: JSON.stringify(parameters),
    },
  };
}

function createDispatcher(overrides: Partial<ToolDispatcher> = {}): ToolDispatcher {
  return {
    isFunctionSupported: jest.fn().mockReturnValue(true),
    executeToolCalls: jest.fn().mockResolvedValue([
      {
        tool_call_id: 'call-add',
        content: { id: 'task-1', content: 'Review invoices' },
      },
    ]),
    ...overrides,
  };
}

describe('FunctionCallingProcessor integration', () => {
  afterAll(() => {
    logger.writeSummary();
  });

  it('executes GPT Todoist tool calls and returns the final assistant response', async () => {
    const toolCalls = [
      createToolCall('call-add', 'add_todoist_task', {
        content: 'Review invoices',
        due_string: 'tomorrow at 9am',
        priority: 4,
      }),
      createToolCall('call-list', 'get_tasks_by_filter', { query: 'today' }),
      createToolCall('call-update', 'update_todoist_task', {
        task_id: 'task-1',
        content: 'Review invoices now',
      }),
      createToolCall('call-complete', 'complete_task', { task_id: 'task-1' }),
      createToolCall('call-delete', 'delete_todoist_task', { task_id: 'task-2' }),
    ];
    const create = jest
      .fn()
      .mockResolvedValueOnce({
        choices: [
          {
            message: {
              content: null,
              tool_calls: toolCalls,
            },
          },
        ],
      });
    const openai = createOpenAI(create);
    const dispatcher = createDispatcher({
      executeToolCalls: jest.fn().mockResolvedValue(
        toolCalls.map((toolCall) => ({
          tool_call_id: toolCall.id,
          content: { ok: true, functionName: toolCall.function.name },
        })),
      ),
    });
    const processor = new FunctionCallingProcessor(dispatcher);

    const result = await processor.processWithFunctionCalling(
      openai,
      'gpt-4o',
      0.7,
      'Add review invoices tomorrow at 9am with high priority',
      'user-1',
    );

    logger.logStep('gpt_tool_call_flow', {
      userMessage: 'Add review invoices tomorrow at 9am with high priority',
      toolCalls,
      finalResponse: result.response,
    });
    expect(result.response).toBe('Done — 5 actions completed.');
    expect(result.usedFunctionCalling).toBe(true);
    expect(result.functionCallsCount).toBe(5);
    expect(dispatcher.executeToolCalls).toHaveBeenCalledWith(
      expect.arrayContaining([
        expect.objectContaining({
          id: 'call-add',
          function: expect.objectContaining({ name: 'add_todoist_task' }),
        }),
      ]),
      'user-1',
      {},
    );
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('filters unsupported tool calls before executing dispatcher calls', async () => {
    const create = jest
      .fn()
      .mockResolvedValueOnce({
        choices: [
          {
            message: {
              content: null,
              tool_calls: [
                createToolCall('call-supported', 'get_tasks_by_filter', { query: 'today' }),
                createToolCall('call-unsupported', 'unsupported_tool', {}),
              ],
            },
          },
        ],
      });
    const dispatcher = createDispatcher({
      isFunctionSupported: jest.fn((name: string) => name === 'get_tasks_by_filter'),
      executeToolCalls: jest.fn().mockResolvedValue([
        { tool_call_id: 'call-supported', content: [{ id: 'task-1' }] },
      ]),
    });
    const processor = new FunctionCallingProcessor(dispatcher);

    const result = await processor.processWithFunctionCalling(
      createOpenAI(create),
      'gpt-4o',
      0.7,
      'Show today tasks',
      'user-1',
    );

    logger.logAssertion('unsupported_tool_filtered', { finalResponse: result.response });
    expect(dispatcher.executeToolCalls).toHaveBeenCalledWith(
      [
        {
          id: 'call-supported',
          type: 'function',
          function: { name: 'get_tasks_by_filter', arguments: JSON.stringify({ query: 'today' }) },
        },
      ],
      'user-1',
      {},
    );
    expect(result.response).toBe('Done.');
    expect(create).toHaveBeenCalledTimes(1);
  });

  it('returns a clear response when no requested tool is supported', async () => {
    const create = jest.fn().mockResolvedValueOnce({
      choices: [
        {
          message: {
            content: null,
            tool_calls: [createToolCall('call-unsupported', 'unsupported_tool', {})],
          },
        },
      ],
    });
    const dispatcher = createDispatcher({
      isFunctionSupported: jest.fn().mockReturnValue(false),
      executeToolCalls: jest.fn(),
    });
    const processor = new FunctionCallingProcessor(dispatcher);

    const result = await processor.processWithFunctionCalling(
      createOpenAI(create),
      'gpt-4o',
      0.7,
      'Do something unsupported',
      'user-1',
    );

    expect(result.response).toBe("I'm not able to perform that action right now.");
    expect(dispatcher.executeToolCalls).not.toHaveBeenCalled();
  });

  it('returns a user-facing error when dispatcher execution fails', async () => {
    const create = jest.fn().mockResolvedValueOnce({
      choices: [
        {
          message: {
            content: null,
            tool_calls: [createToolCall('call-add', 'add_todoist_task', { content: 'Task' })],
          },
        },
      ],
    });
    const dispatcher = createDispatcher({
      executeToolCalls: jest.fn().mockRejectedValue(new Error('dispatcher exploded')),
    });
    const processor = new FunctionCallingProcessor(dispatcher);

    const result = await processor.processWithFunctionCalling(
      createOpenAI(create),
      'gpt-4o',
      0.7,
      'Add a task',
      'user-1',
    );

    expect(result.response).toContain('dispatcher exploded');
  });

  it('formats dispatcher-reported validation failures without a final GPT call', async () => {
    const create = jest.fn().mockResolvedValueOnce({
      choices: [
        {
          message: {
            content: null,
            tool_calls: [createToolCall('call-add', 'add_todoist_task', { priority: 5 })],
          },
        },
      ],
    });
    const dispatcher = createDispatcher({
      executeToolCalls: jest.fn().mockResolvedValue([
        {
          tool_call_id: 'call-add',
          toolName: 'add_todoist_task',
          content: null,
          error: 'Invalid arguments for add_todoist_task',
        },
      ]),
    });
    const processor = new FunctionCallingProcessor(dispatcher);

    const result = await processor.processWithFunctionCalling(
      createOpenAI(create),
      'gpt-4o',
      0.7,
      'Add a task',
      'user-1',
    );

    expect(result.response).toBe(
      'Failed:\n• add_todoist_task: Invalid arguments for add_todoist_task',
    );
    expect(create).toHaveBeenCalledTimes(1);
  });
});
