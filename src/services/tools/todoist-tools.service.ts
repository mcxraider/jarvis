/**
 * Tool definitions for OpenAI function calling
 *
 * @module GPTTools
 */

import OpenAI from 'openai';
import { ToolDispatcher } from '../../types/tool.types';

/**
 * Service for managing and providing tool definitions for GPT function calling
 */
export class GPTToolsService {
  constructor(private readonly toolDispatcher?: ToolDispatcher) {}

  /**
   * Get available tools/functions for OpenAI function calling
   *
   * @returns Array of tool definitions for OpenAI
   */
  getAvailableTools(): OpenAI.Chat.Completions.ChatCompletionTool[] {
    if (!this.toolDispatcher) {
      return [];
    }

    // Define comprehensive Todoist tools available through the tool dispatcher
    // Priority enum reversed: 1 is urgent, 4 is normal
    return [
      {
        type: 'function',
        function: {
          name: 'add_todoist_task',
          description: 'Add a new task to Todoist with specified details',
          parameters: {
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
                description:
                  'Project ID where task should be created (optional, defaults to Inbox)',
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
                description: 'Priority level: 1 (urgent), 2 (very high), 3 (high), 4 (normal)',
              },
              due_string: {
                type: 'string',
                description:
                  'Natural language due date (e.g., "tomorrow", "next Monday", "in 2 weeks")',
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
          },
        },
      },
      {
        type: 'function',
        function: {
          name: 'get_todoist_task',
          description: 'Retrieve a specific task from Todoist by its ID',
          parameters: {
            type: 'object',
            properties: {
              task_id: {
                type: 'string',
                description: 'The unique ID of the task to retrieve',
              },
            },
            required: ['task_id'],
            additionalProperties: false,
          },
        },
      },
      {
        type: 'function',
        function: {
          name: 'update_todoist_task',
          description: 'Update an existing task in Todoist',
          parameters: {
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
                description: 'Priority level: 1 (urgent), 2 (very high), 3 (high), 4 (normal)',
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
          },
        },
      },
      {
        type: 'function',
        function: {
          name: 'delete_todoist_task',
          description: 'Delete a task from Todoist permanently',
          parameters: {
            type: 'object',
            properties: {
              task_id: {
                type: 'string',
                description: 'The unique ID of the task to delete',
              },
            },
            required: ['task_id'],
            additionalProperties: false,
          },
        },
      },
      {
        type: 'function',
        function: {
          name: 'get_completed_todoist_tasks',
          description:
            'Retrieve completed tasks from Todoist within a date range using the Sync API',
          parameters: {
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
              limit: {
                type: 'integer',
                minimum: 1,
                maximum: 200,
                default: 50,
                description: 'Maximum number of completed tasks to return (1-200, default 50)',
              },
              offset: {
                type: 'integer',
                minimum: 0,
                default: 0,
                description: 'Offset for pagination (default 0)',
              },
            },
            required: [],
            additionalProperties: false,
          },
        },
      },
      {
        type: 'function',
        function: {
          name: 'get_tasks',
          description: 'Retrieve existing tasks from Todoist, optionally filtered',
          parameters: {
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
            additionalProperties: false,
          },
        },
      },
      {
        type: 'function',
        function: {
          name: 'complete_task',
          description: 'Mark a task as completed in Todoist',
          parameters: {
            type: 'object',
            properties: {
              task_id: {
                type: 'string',
                description: 'The ID of the task to complete',
              },
            },
            required: ['task_id'],
            additionalProperties: false,
          },
        },
      },
    ];
  }

  /**
   * Get function names for easier reference
   *
   * @returns Array of available function names
   */
  getAvailableFunctionNames(): string[] {
    return this.getAvailableTools().map((tool) => tool.function.name);
  }

  /**
   * Get a specific tool definition by name
   *
   * @param functionName - Name of the function to retrieve
   * @returns Tool definition or undefined if not found
   */
  getToolByName(functionName: string): OpenAI.Chat.Completions.ChatCompletionTool | undefined {
    return this.getAvailableTools().find((tool) => tool.function.name === functionName);
  }

  /**
   * Validate if a function name is supported
   *
   * @param functionName - Name of the function to validate
   * @returns True if function is supported, false otherwise
   */
  isFunctionSupported(functionName: string): boolean {
    return this.getAvailableFunctionNames().includes(functionName);
  }
}
