import { ToolArgumentValidatorService } from '../../../../src/services/tools/tool-argument-validator.service';
import { ToolCall } from '../../../../src/types/tool.types';

const createToolCall = (
  name: string,
  parameters: Record<string, unknown> | string,
): ToolCall => ({
  id: `call-${name}`,
  type: 'function',
  function: {
    name,
    arguments: typeof parameters === 'string' ? parameters : JSON.stringify(parameters),
  },
});

describe('ToolArgumentValidatorService', () => {
  const validator = new ToolArgumentValidatorService();

  it('validates supported Todoist tool arguments', () => {
    expect(
      validator.validate(
        createToolCall('add_todoist_task', {
          content: 'Pay rent',
          priority: 4,
          due_date: '2026-06-17',
        }),
      ),
    ).toEqual({
      functionName: 'add_todoist_task',
      arguments: {
        content: 'Pay rent',
        priority: 4,
        due_date: '2026-06-17',
      },
    });

    expect(
      validator.validate(
        createToolCall('update_todoist_task', {
          task_id: 'task-1',
          content: 'Pay rent now',
        }),
      ).functionName,
    ).toBe('update_todoist_task');
    expect(validator.validate(createToolCall('get_tasks', { filter: 'today' })).functionName).toBe(
      'get_tasks',
    );
    expect(validator.validate(createToolCall('complete_task', { task_id: 'task-1' })).functionName).toBe(
      'complete_task',
    );
    expect(
      validator.validate(createToolCall('delete_todoist_task', { task_id: 'task-1' })).functionName,
    ).toBe('delete_todoist_task');
  });

  it('rejects malformed JSON', () => {
    expect(() => validator.validate(createToolCall('add_todoist_task', '{bad json'))).toThrow(
      'Invalid JSON arguments for add_todoist_task',
    );
  });

  it('rejects unknown extra keys', () => {
    expect(() =>
      validator.validate(
        createToolCall('add_todoist_task', {
          content: 'Pay rent',
          unexpected: true,
        }),
      ),
    ).toThrow('Invalid arguments for add_todoist_task');
  });

  it('rejects missing required fields', () => {
    expect(() => validator.validate(createToolCall('add_todoist_task', {}))).toThrow(
      'Invalid arguments for add_todoist_task',
    );
    expect(() => validator.validate(createToolCall('update_todoist_task', { content: 'Task' }))).toThrow(
      'Invalid arguments for update_todoist_task',
    );
  });

  it('rejects invalid priority, due_date, limit, and offset values', () => {
    expect(() =>
      validator.validate(createToolCall('add_todoist_task', { content: 'Task', priority: 5 })),
    ).toThrow('Invalid arguments for add_todoist_task');
    expect(() =>
      validator.validate(createToolCall('add_todoist_task', { content: 'Task', due_date: 'tomorrow' })),
    ).toThrow('Invalid arguments for add_todoist_task');
    expect(() =>
      validator.validate(createToolCall('get_completed_todoist_tasks', { limit: 201 })),
    ).toThrow('Invalid arguments for get_completed_todoist_tasks');
    expect(() =>
      validator.validate(createToolCall('get_completed_todoist_tasks', { offset: -1 })),
    ).toThrow('Invalid arguments for get_completed_todoist_tasks');
  });

  it('rejects unsupported function names', () => {
    expect(() => validator.validate(createToolCall('unknown_function', {}))).toThrow(
      'Unknown function: unknown_function',
    );
  });
});
