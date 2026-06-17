import { ToolResultFormatter } from '../../../../../src/services/telegram/formatters/tool-result-formatter';

describe('ToolResultFormatter', () => {
  const formatter = new ToolResultFormatter();

  it('formats a single success', () => {
    expect(
      formatter.formatToolResults([
        { tool_call_id: 'call-1', toolName: 'add_todoist_task', content: { id: 'task-1' } },
      ]),
    ).toBe('Done.');
  });

  it('formats multiple successes', () => {
    expect(
      formatter.formatToolResults([
        { tool_call_id: 'call-1', toolName: 'add_todoist_task', content: { id: 'task-1' } },
        { tool_call_id: 'call-2', toolName: 'get_tasks', content: [] },
      ]),
    ).toBe('Done — 2 actions completed.');
  });

  it('formats partial failure', () => {
    expect(
      formatter.formatToolResults([
        { tool_call_id: 'call-1', toolName: 'add_todoist_task', content: { id: 'task-1' } },
        {
          tool_call_id: 'call-2',
          toolName: 'delete_todoist_task',
          content: null,
          error: 'Todoist API error (404): missing',
        },
      ]),
    ).toBe(
      '1/2 completed.\n\n<b>Failed:</b>\n• delete_todoist_task: Todoist API error (404): missing',
    );
  });

  it('formats total failure', () => {
    expect(
      formatter.formatToolResults([
        {
          tool_call_id: 'call-1',
          toolName: 'add_todoist_task',
          content: null,
          error: 'Invalid arguments for add_todoist_task',
        },
      ]),
    ).toBe('Failed:\n• add_todoist_task: Invalid arguments for add_todoist_task');
  });

  it('uses display labels when available', () => {
    expect(
      formatter.formatToolResults([
        {
          tool_call_id: 'call-1',
          toolName: 'add_todoist_task',
          displayLabel: 'Create rent task',
          content: null,
          error: 'Todoist unavailable',
        },
      ]),
    ).toBe('Failed:\n• Create rent task: Todoist unavailable');
  });
});
