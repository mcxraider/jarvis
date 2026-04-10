/**
 * Direct tool call dispatcher that executes Todoist operations via REST API
 * Bypasses MCP servers for direct API integration
 *
 * @module DirectToolCallDispatcher
 */

import { logger } from '../../utils/logger';
import { ToolCall, ToolResult, ToolDispatcher } from '../../types/mcp.types';
import { TodoistAPIService } from '../external/todoist-api.service';

/**
 * Service for dispatching tool calls directly to external APIs
 */
export class DirectToolCallDispatcher implements ToolDispatcher {
  private readonly todoistService: TodoistAPIService;

  constructor() {
    const todoistApiKey = process.env.TODOIST_API_KEY;
    if (!todoistApiKey) {
      throw new Error('TODOIST_API_KEY environment variable is required');
    }

    this.todoistService = new TodoistAPIService(todoistApiKey);

    logger.info('DirectToolCallDispatcher initialized', {
      hasTodoistService: !!this.todoistService,
    });
  }

  /**
   * Executes multiple tool calls in parallel and returns results
   *
   * @param toolCalls - Array of function calls from GPT
   * @param userId - User ID for context/authorization
   * @returns Promise<ToolResult[]> - Results of all tool executions
   */
  async executeToolCalls(toolCalls: ToolCall[], userId: string): Promise<ToolResult[]> {
    logger.info('Executing tool calls directly', {
      userId,
      toolCallsCount: toolCalls.length,
      functionNames: toolCalls.map((tc) => tc.function.name),
    });

    // Execute all tool calls in parallel for better performance
    const promises = toolCalls.map((toolCall) => this.executeToolCall(toolCall, userId));
    const results = await Promise.allSettled(promises);

    // Convert Promise.allSettled results to our ToolResult format
    return results.map((result, index) => {
      const toolCallId = toolCalls[index].id;

      if (result.status === 'fulfilled') {
        return {
          tool_call_id: toolCallId,
          content: result.value,
        };
      } else {
        return {
          tool_call_id: toolCallId,
          content: null,
          error: result.reason?.message || 'Tool execution failed',
        };
      }
    });
  }

  /**
   * Executes a single tool call by routing to the appropriate service
   *
   * @param toolCall - The function call to execute
   * @param userId - User ID for context
   * @returns Promise<any> - The result of the function execution
   */
  private async executeToolCall(toolCall: ToolCall, userId: string): Promise<any> {
    try {
      // Extract function details from the tool call
      const functionName = toolCall.function.name;
      const parameters = JSON.parse(toolCall.function.arguments);

      logger.debug('Executing tool call', {
        userId,
        functionName,
        toolCallId: toolCall.id,
        parameters: Object.keys(parameters),
      });

      // Route function calls to appropriate handlers
      const result = await this.routeFunctionCall(functionName, parameters);

      logger.debug('Tool call executed successfully', {
        userId,
        functionName,
        toolCallId: toolCall.id,
        hasResult: !!result,
      });

      return result;
    } catch (error) {
      logger.error('Tool call execution error', {
        userId,
        functionName: toolCall.function.name,
        toolCallId: toolCall.id,
        error: (error as Error).message,
      });
      throw error;
    }
  }

  /**
   * Routes function calls to appropriate service methods
   *
   * @param functionName - Name of the function to call
   * @param parameters - Function parameters
   * @returns Promise<any> - Function result
   */
  private async routeFunctionCall(functionName: string, parameters: any): Promise<any> {
    switch (functionName) {
      case 'add_todoist_task':
        return await this.todoistService.addTask({
          content: parameters.content,
          description: parameters.description,
          project_id: parameters.project_id,
          section_id: parameters.section_id,
          parent_id: parameters.parent_id,
          order: parameters.order,
          labels: parameters.labels,
          priority: parameters.priority,
          due_string: parameters.due_string,
          due_date: parameters.due_date,
          due_datetime: parameters.due_datetime,
          assignee_id: parameters.assignee_id,
        });

      case 'get_todoist_task':
        return await this.todoistService.getTask(parameters.task_id);

      case 'get_tasks':
        return await this.todoistService.getTasks({
          project_id: parameters.project_id,
          section_id: parameters.section_id,
          label: parameters.label,
          filter: parameters.filter,
          lang: parameters.lang,
          ids: parameters.ids,
        });

      case 'update_todoist_task':
        return await this.todoistService.updateTask(parameters.task_id, {
          content: parameters.content,
          description: parameters.description,
          labels: parameters.labels,
          priority: parameters.priority,
          due_string: parameters.due_string,
          due_date: parameters.due_date,
          due_datetime: parameters.due_datetime,
          assignee_id: parameters.assignee_id,
        });

      case 'complete_task':
        await this.todoistService.completeTask(parameters.task_id);
        return { success: true, message: `Task ${parameters.task_id} marked as completed` };

      case 'delete_todoist_task':
        await this.todoistService.deleteTask(parameters.task_id);
        return { success: true, message: `Task ${parameters.task_id} deleted permanently` };

      case 'get_completed_todoist_tasks':
        return await this.todoistService.getCompletedTasks({
          since: parameters.since,
          until: parameters.until,
          project_id: parameters.project_id,
          limit: parameters.limit,
          offset: parameters.offset,
        });

      default:
        throw new Error(`Unknown function: ${functionName}`);
    }
  }

  /**
   * Get list of supported function names
   *
   * @returns string[] - Array of supported function names
   */
  getSupportedFunctions(): string[] {
    return [
      'add_todoist_task',
      'get_todoist_task',
      'get_tasks',
      'update_todoist_task',
      'complete_task',
      'delete_todoist_task',
      'get_completed_todoist_tasks',
    ];
  }

  /**
   * Check if a function is supported
   *
   * @param functionName - Function name to check
   * @returns boolean - True if supported
   */
  isFunctionSupported(functionName: string): boolean {
    return this.getSupportedFunctions().includes(functionName);
  }
}
