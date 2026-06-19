import OpenAI from 'openai';
import { z } from 'zod';

const dueDateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Expected YYYY-MM-DD format');
const prioritySchema = z.union([z.literal(1), z.literal(2), z.literal(3), z.literal(4)]);
const stringArraySchema = z.array(z.string());

export const todoistToolArgumentSchemas = {
  add_todoist_task: z
    .object({
      content: z.string(),
      description: z.string().optional(),
      project_id: z.string().optional(),
      section_id: z.string().optional(),
      parent_id: z.string().optional(),
      order: z.number().int().optional(),
      labels: stringArraySchema.optional(),
      priority: prioritySchema.optional(),
      due_string: z.string().optional(),
      due_date: dueDateSchema.optional(),
      due_datetime: z.string().optional(),
      assignee_id: z.string().optional(),
    })
    .strict(),
  get_todoist_task: z
    .object({
      task_id: z.string(),
    })
    .strict(),
  update_todoist_task: z
    .object({
      task_id: z.string(),
      content: z.string().optional(),
      description: z.string().optional(),
      labels: stringArraySchema.optional(),
      priority: prioritySchema.optional(),
      due_string: z.string().optional(),
      due_date: dueDateSchema.optional(),
      due_datetime: z.string().optional(),
      assignee_id: z.string().optional(),
    })
    .strict(),
  delete_todoist_task: z
    .object({
      task_id: z.string(),
    })
    .strict(),
  get_completed_todoist_tasks_by_completion_date: z
    .object({
      since: z.string().optional(),
      until: z.string().optional(),
      project_id: z.string().optional(),
      section_id: z.string().optional(),
      parent_id: z.string().optional(),
      filter_query: z.string().optional(),
      filter_lang: z.string().optional(),
      cursor: z.string().optional(),
      limit: z.number().int().min(1).max(200).optional(),
    })
    .strict(),
  get_tasks: z
    .object({
      project_id: z.string().optional(),
      section_id: z.string().optional(),
      label: z.string().optional(),
      filter: z.string().optional(),
      lang: z.string().optional(),
      ids: stringArraySchema.optional(),
    })
    .strict(),
  complete_task: z
    .object({
      task_id: z.string(),
    })
    .strict(),
};

export type TodoistToolName = keyof typeof todoistToolArgumentSchemas;
export type TodoistToolArguments = z.infer<(typeof todoistToolArgumentSchemas)[TodoistToolName]>;

export const TODOIST_TOOL_NAMES = Object.keys(
  todoistToolArgumentSchemas,
) as TodoistToolName[];

const addTaskParameters = {
  type: 'object',
  properties: {
    content: {
      type: 'string',
      description: 'The task title or content (required)',
    },
    description: {
      type: 'string',
      description: 'Optional task description with additional details',
    },
    project_id: {
      type: 'string',
      description: 'Project ID where task should be created (optional, defaults to Inbox)',
    },
    section_id: {
      type: 'string',
      description: 'Section ID within the project (optional)',
    },
    parent_id: {
      type: 'string',
      description: 'Parent task ID to create a subtask (optional)',
    },
    order: {
      type: 'integer',
      description: 'Task order within the project/section (optional)',
    },
    labels: {
      type: 'array',
      items: {
        type: 'string',
      },
      description: 'Array of label names to assign to the task (optional)',
    },
    priority: {
      type: 'integer',
      enum: [1, 2, 3, 4],
      description: 'Priority level: 1 (normal / no priority), 2 (low), 3 (medium), 4 (high / urgent)',
    },
    due_string: {
      type: 'string',
      description: 'Natural language due date (e.g., "tomorrow", "next Monday", "in 2 weeks")',
    },
    due_date: {
      type: 'string',
      pattern: '^\\d{4}-\\d{2}-\\d{2}$',
      description: 'Due date in YYYY-MM-DD format',
    },
    due_datetime: {
      type: 'string',
      description: 'Due datetime in RFC3339 format (e.g., "2024-06-15T14:30:00Z")',
    },
    assignee_id: {
      type: 'string',
      description: 'User ID to assign the task to (for shared projects)',
    },
  },
  required: ['content'],
  additionalProperties: false,
} as const;

const taskIdParameters = (description: string) =>
  ({
    type: 'object',
    properties: {
      task_id: {
        type: 'string',
        description,
      },
    },
    required: ['task_id'],
    additionalProperties: false,
  }) as const;

const updateTaskParameters = {
  type: 'object',
  properties: {
    task_id: {
      type: 'string',
      description: 'The unique ID of the task to update',
    },
    content: {
      type: 'string',
      description: 'New task title/content',
    },
    description: {
      type: 'string',
      description: 'New task description',
    },
    labels: {
      type: 'array',
      items: {
        type: 'string',
      },
      description: 'Array of label names to assign to the task',
    },
    priority: {
      type: 'integer',
      enum: [1, 2, 3, 4],
      description: 'Priority level: 1 (normal / no priority), 2 (low), 3 (medium), 4 (high / urgent)',
    },
    due_string: {
      type: 'string',
      description: 'Natural language due date (e.g., "tomorrow", "next Monday")',
    },
    due_date: {
      type: 'string',
      pattern: '^\\d{4}-\\d{2}-\\d{2}$',
      description: 'Due date in YYYY-MM-DD format',
    },
    due_datetime: {
      type: 'string',
      description: 'Due datetime in RFC3339 format',
    },
    assignee_id: {
      type: 'string',
      description: 'User ID to assign the task to',
    },
  },
  required: ['task_id'],
  additionalProperties: false,
} as const;

const completedTasksParameters = {
  type: 'object',
  properties: {
    since: {
      type: 'string',
      description: 'Start date in ISO 8601 format (e.g., "2024-01-01T00:00:00Z")',
    },
    until: {
      type: 'string',
      description: 'End date in ISO 8601 format (e.g., "2024-01-31T23:59:59Z")',
    },
    project_id: {
      type: 'string',
      description: 'Filter completed tasks by specific project ID (optional)',
    },
    section_id: {
      type: 'string',
      description: 'Filter completed tasks by specific section ID (optional)',
    },
    parent_id: {
      type: 'string',
      description: 'Filter completed subtasks by parent task ID (optional)',
    },
    filter_query: {
      type: 'string',
      description: 'Todoist filter query to limit completed tasks (optional)',
    },
    filter_lang: {
      type: 'string',
      description: 'Language code used to parse filter_query (optional)',
    },
    cursor: {
      type: 'string',
      description: 'Pagination cursor returned as next_cursor from a previous response',
    },
    limit: {
      type: 'integer',
      minimum: 1,
      maximum: 200,
      default: 50,
      description: 'Maximum number of completed tasks to return (1-200, default 50)',
    },
  },
  required: [],
  additionalProperties: false,
} as const;

const getTasksParameters = {
  type: 'object',
  properties: {
    project_id: {
      type: 'string',
      description: 'Filter tasks by project ID',
    },
    section_id: {
      type: 'string',
      description: 'Filter tasks by section ID',
    },
    label: {
      type: 'string',
      description: 'Filter tasks by label name',
    },
    filter: {
      type: 'string',
      description: 'Todoist filter expression (e.g., "today", "overdue", "p1")',
    },
    lang: {
      type: 'string',
      description: 'Language code for filter (default: en)',
    },
    ids: {
      type: 'array',
      items: {
        type: 'string',
      },
      description: 'Array of task IDs to retrieve specific tasks',
    },
  },
  required: [],
  additionalProperties: false,
} as const;

export const TODOIST_TOOL_DEFINITIONS: OpenAI.Chat.Completions.ChatCompletionTool[] = [
  {
    type: 'function',
    function: {
      name: 'add_todoist_task',
      description: 'Add a new task to Todoist with specified details',
      parameters: addTaskParameters,

    },
  },
  {
    type: 'function',
    function: {
      name: 'get_todoist_task',
      description: 'Retrieve a specific task from Todoist by its ID',
      parameters: taskIdParameters('The unique ID of the task to retrieve'),

    },
  },
  {
    type: 'function',
    function: {
      name: 'update_todoist_task',
      description: 'Update an existing task in Todoist',
      parameters: updateTaskParameters,

    },
  },
  {
    type: 'function',
    function: {
      name: 'delete_todoist_task',
      description: 'Delete a task from Todoist permanently',
      parameters: taskIdParameters('The unique ID of the task to delete'),

    },
  },
  {
    type: 'function',
    function: {
      name: 'get_completed_todoist_tasks_by_completion_date',
      description: 'Retrieve completed Todoist tasks by completion date using Todoist API v1',
      parameters: completedTasksParameters,

    },
  },
  {
    type: 'function',
    function: {
      name: 'get_tasks',
      description: 'Retrieve existing tasks from Todoist, optionally filtered',
      parameters: getTasksParameters,

    },
  },
  {
    type: 'function',
    function: {
      name: 'complete_task',
      description: 'Mark a task as completed in Todoist',
      parameters: taskIdParameters('The ID of the task to complete'),

    },
  },
];
