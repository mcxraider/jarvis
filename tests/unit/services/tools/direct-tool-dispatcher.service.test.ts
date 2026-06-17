import { DirectToolCallDispatcher } from '../../../../src/services/tools/direct-tool-dispatcher.service';
import { TodoistAPIService } from '../../../../src/services/external/todoist-api.service';
import { ToolCall } from '../../../../src/types/tool.types';

jest.mock('../../../../src/services/external/todoist-api.service');

const mockTodoistService = {
  addTask: jest.fn(),
  getTask: jest.fn(),
  getTasks: jest.fn(),
  updateTask: jest.fn(),
  completeTask: jest.fn(),
  deleteTask: jest.fn(),
  getCompletedTasks: jest.fn(),
};

const MockTodoistAPIService = TodoistAPIService as jest.MockedClass<typeof TodoistAPIService>;

const createToolCall = (
  id: string,
  name: string,
  parameters: Record<string, unknown> | string,
): ToolCall => ({
  id,
  type: 'function',
  function: {
    name,
    arguments: typeof parameters === 'string' ? parameters : JSON.stringify(parameters),
  },
});

describe('DirectToolCallDispatcher', () => {
  const originalTodoistApiKey = process.env.TODOIST_API_KEY;

  beforeEach(() => {
    process.env.TODOIST_API_KEY = 'todoist-test-key';
    jest.clearAllMocks();
    MockTodoistAPIService.mockImplementation(() => mockTodoistService as unknown as TodoistAPIService);
  });

  afterAll(() => {
    if (originalTodoistApiKey === undefined) {
      delete process.env.TODOIST_API_KEY;
    } else {
      process.env.TODOIST_API_KEY = originalTodoistApiKey;
    }
  });

  it('forwards all supported create fields to Todoist', async () => {
    const createdTask = { id: 'task-1', content: 'Pay rent' };
    mockTodoistService.addTask.mockResolvedValue(createdTask);
    const dispatcher = new DirectToolCallDispatcher();

    const result = await dispatcher.executeToolCalls(
      [
        createToolCall('call-1', 'add_todoist_task', {
          content: 'Pay rent',
          description: 'Before noon',
          project_id: 'project-1',
          section_id: 'section-1',
          parent_id: 'parent-1',
          order: 3,
          labels: ['home', 'money'],
          priority: 4,
          due_string: 'tomorrow',
          due_date: '2026-06-17',
          due_datetime: '2026-06-17T02:00:00Z',
          assignee_id: 'user-1',
        }),
      ],
      'user-1',
    );

    expect(mockTodoistService.addTask).toHaveBeenCalledWith(
      {
        content: 'Pay rent',
        description: 'Before noon',
        project_id: 'project-1',
        section_id: 'section-1',
        parent_id: 'parent-1',
        order: 3,
        labels: ['home', 'money'],
        priority: 4,
        due_string: 'tomorrow',
        due_date: '2026-06-17',
        due_datetime: '2026-06-17T02:00:00Z',
        assignee_id: 'user-1',
      },
      {},
    );
    expect(result).toEqual([
      { tool_call_id: 'call-1', toolName: 'add_todoist_task', content: createdTask },
    ]);
  });

  it('forwards update task fields to Todoist', async () => {
    const updatedTask = { id: 'task-1', content: 'Pay rent now' };
    mockTodoistService.updateTask.mockResolvedValue(updatedTask);
    const dispatcher = new DirectToolCallDispatcher();

    const result = await dispatcher.executeToolCalls(
      [
        createToolCall('call-1', 'update_todoist_task', {
          task_id: 'task-1',
          content: 'Pay rent now',
          description: 'Late fee tomorrow',
          labels: ['urgent'],
          priority: 4,
          due_string: 'today',
          due_date: '2026-06-16',
          due_datetime: '2026-06-16T10:00:00Z',
          assignee_id: 'user-1',
        }),
      ],
      'user-1',
    );

    expect(mockTodoistService.updateTask).toHaveBeenCalledWith(
      'task-1',
      {
        content: 'Pay rent now',
        description: 'Late fee tomorrow',
        labels: ['urgent'],
        priority: 4,
        due_string: 'today',
        due_date: '2026-06-16',
        due_datetime: '2026-06-16T10:00:00Z',
        assignee_id: 'user-1',
      },
      {},
    );
    expect(result).toEqual([
      { tool_call_id: 'call-1', toolName: 'update_todoist_task', content: updatedTask },
    ]);
  });

  it('routes read, complete, delete, and completed-task calls', async () => {
    mockTodoistService.getTask.mockResolvedValue({ id: 'task-1' });
    mockTodoistService.getTasks.mockResolvedValue([{ id: 'task-2' }]);
    mockTodoistService.completeTask.mockResolvedValue(undefined);
    mockTodoistService.deleteTask.mockResolvedValue(undefined);
    mockTodoistService.getCompletedTasks.mockResolvedValue([{ id: 'done-1' }]);
    const dispatcher = new DirectToolCallDispatcher();

    const result = await dispatcher.executeToolCalls(
      [
        createToolCall('get-one', 'get_todoist_task', { task_id: 'task-1' }),
        createToolCall('get-many', 'get_tasks', {
          project_id: 'project-1',
          section_id: 'section-1',
          label: 'home',
          filter: 'today',
          lang: 'en',
          ids: ['task-1', 'task-2'],
        }),
        createToolCall('complete', 'complete_task', { task_id: 'task-3' }),
        createToolCall('delete', 'delete_todoist_task', { task_id: 'task-4' }),
        createToolCall('completed', 'get_completed_todoist_tasks', {
          since: '2026-06-01T00:00:00Z',
          until: '2026-06-16T23:59:59Z',
          project_id: 'project-1',
          limit: 25,
          offset: 5,
        }),
      ],
      'user-1',
    );

    expect(mockTodoistService.getTask).toHaveBeenCalledWith('task-1', {});
    expect(mockTodoistService.getTasks).toHaveBeenCalledWith(
      {
        project_id: 'project-1',
        section_id: 'section-1',
        label: 'home',
        filter: 'today',
        lang: 'en',
        ids: ['task-1', 'task-2'],
      },
      {},
    );
    expect(mockTodoistService.completeTask).toHaveBeenCalledWith('task-3', {});
    expect(mockTodoistService.deleteTask).toHaveBeenCalledWith('task-4', {});
    expect(mockTodoistService.getCompletedTasks).toHaveBeenCalledWith(
      {
        since: '2026-06-01T00:00:00Z',
        until: '2026-06-16T23:59:59Z',
        project_id: 'project-1',
        limit: 25,
        offset: 5,
      },
      {},
    );
    expect(result).toEqual([
      { tool_call_id: 'get-one', toolName: 'get_todoist_task', content: { id: 'task-1' } },
      { tool_call_id: 'get-many', toolName: 'get_tasks', content: [{ id: 'task-2' }] },
      {
        tool_call_id: 'complete',
        toolName: 'complete_task',
        content: { success: true, message: 'Task task-3 marked as completed' },
      },
      {
        tool_call_id: 'delete',
        toolName: 'delete_todoist_task',
        content: { success: true, message: 'Task task-4 deleted permanently' },
      },
      {
        tool_call_id: 'completed',
        toolName: 'get_completed_todoist_tasks',
        content: [{ id: 'done-1' }],
      },
    ]);
  });

  it('returns ToolResult errors for malformed JSON and unknown functions', async () => {
    const dispatcher = new DirectToolCallDispatcher();

    const result = await dispatcher.executeToolCalls(
      [
        createToolCall('bad-json', 'add_todoist_task', '{bad json'),
        createToolCall('unknown', 'unknown_function', {}),
      ],
      'user-1',
    );

    expect(result).toHaveLength(2);
    expect(result[0].tool_call_id).toBe('bad-json');
    expect(result[0].content).toBeNull();
    expect(result[0].error).toBeTruthy();
    expect(result[1]).toEqual({
      tool_call_id: 'unknown',
      toolName: 'unknown_function',
      content: null,
      error: 'Unknown function: unknown_function',
    });
  });

  it('returns ToolResult errors for invalid schema arguments without calling Todoist', async () => {
    const dispatcher = new DirectToolCallDispatcher();

    const result = await dispatcher.executeToolCalls(
      [
        createToolCall('invalid-priority', 'add_todoist_task', {
          content: 'Task',
          priority: 5,
        }),
      ],
      'user-1',
    );

    expect(mockTodoistService.addTask).not.toHaveBeenCalled();
    expect(result).toHaveLength(1);
    expect(result[0].tool_call_id).toBe('invalid-priority');
    expect(result[0].toolName).toBe('add_todoist_task');
    expect(result[0].content).toBeNull();
    expect(result[0].error).toContain('Invalid arguments for add_todoist_task');
  });

  it('preserves tool call IDs when some calls fail', async () => {
    mockTodoistService.getTask.mockResolvedValue({ id: 'task-1' });
    mockTodoistService.deleteTask.mockRejectedValue(new Error('Todoist API error (404): missing'));
    const dispatcher = new DirectToolCallDispatcher();

    const result = await dispatcher.executeToolCalls(
      [
        createToolCall('success-call', 'get_todoist_task', { task_id: 'task-1' }),
        createToolCall('failed-call', 'delete_todoist_task', { task_id: 'missing-task' }),
      ],
      'user-1',
    );

    expect(result).toEqual([
      {
        tool_call_id: 'success-call',
        toolName: 'get_todoist_task',
        content: { id: 'task-1' },
      },
      {
        tool_call_id: 'failed-call',
        toolName: 'delete_todoist_task',
        content: null,
        error: 'Todoist API error (404): missing',
      },
    ]);
  });
});
