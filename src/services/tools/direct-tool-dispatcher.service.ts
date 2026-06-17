/**
 * Direct tool call dispatcher that executes Todoist operations via REST API
 *
 * @module DirectToolCallDispatcher
 */

import { logger } from '../../utils/logger';
import { ToolCall, ToolResult, ToolDispatcher } from '../../types/tool.types';
import { TodoistAPIService } from '../external/todoist-api.service';
import { LogContext } from '../../utils/logger';
import { ToolArgumentValidatorService } from './tool-argument-validator.service';
import { TODOIST_TOOL_NAMES } from './todoist-tool-schemas';

/**
 * Service for dispatching tool calls directly to external APIs
 */
export class DirectToolCallDispatcher implements ToolDispatcher {
  private readonly todoistService: TodoistAPIService;
  private readonly argumentValidator = new ToolArgumentValidatorService();

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
  async executeToolCalls(
    toolCalls: ToolCall[],
    userId: string,
    logContext: LogContext = {},
  ): Promise<ToolResult[]> {
    const startedAt = Date.now();

    logger.info('tool.dispatch.started', {
      ...logContext,
      userId,
      toolCallsCount: toolCalls.length,
      functionNames: toolCalls.map((tc) => tc.function.name),
    });

    // Execute all tool calls in parallel for better performance
    const promises = toolCalls.map((toolCall) => this.executeToolCall(toolCall, userId, logContext));
    const results = await Promise.allSettled(promises);

    // Convert Promise.allSettled results to our ToolResult format
    const toolResults = results.map((result, index) => {
      const toolCallId = toolCalls[index].id;

      if (result.status === 'fulfilled') {
        return {
          tool_call_id: toolCallId,
          toolName: toolCalls[index].function.name,
          content: result.value,
        };
      } else {
        return {
          tool_call_id: toolCallId,
          toolName: toolCalls[index].function.name,
          content: null,
          error: result.reason?.message || 'Tool execution failed',
        };
      }
    });

    logger.info('tool.dispatch.completed', {
      ...logContext,
      userId,
      toolCallsCount: toolCalls.length,
      successCount: toolResults.filter((result) => !result.error).length,
      failureCount: toolResults.filter((result) => !!result.error).length,
      durationMs: Date.now() - startedAt,
    });

    return toolResults;
  }

  /**
   * Executes a single tool call by routing to the appropriate service
   *
   * @param toolCall - The function call to execute
   * @param userId - User ID for context
   * @returns Promise<any> - The result of the function execution
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async executeToolCall(
    toolCall: ToolCall,
    userId: string,
    logContext: LogContext,
  ): Promise<any> {
    const startedAt = Date.now();

    try {
      const { functionName, arguments: parameters } = this.argumentValidator.validate(toolCall);

      logger.debug('tool.call.started', {
        ...logContext,
        userId,
        functionName,
        toolCallId: toolCall.id,
        parameters: Object.keys(parameters),
      });

      const result = await this.routeFunctionCall(functionName, parameters, logContext);

      logger.info('tool.call.completed', {
        ...logContext,
        userId,
        functionName,
        toolCallId: toolCall.id,
        hasResult: !!result,
        durationMs: Date.now() - startedAt,
      });

      return result;
    } catch (error) {
      logger.error('tool.call.failed', {
        ...logContext,
        userId,
        functionName: toolCall.function.name,
        toolCallId: toolCall.id,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
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
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async routeFunctionCall(
    functionName: string,
    parameters: any,
    logContext: LogContext,
  ): Promise<any> {
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
        }, logContext);

      case 'get_todoist_task':
        return await this.todoistService.getTask(parameters.task_id, logContext);

      case 'get_tasks':
        return await this.todoistService.getTasks({
          project_id: parameters.project_id,
          section_id: parameters.section_id,
          label: parameters.label,
          filter: parameters.filter,
          lang: parameters.lang,
          ids: parameters.ids,
        }, logContext);

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
        }, logContext);

      case 'complete_task':
        await this.todoistService.completeTask(parameters.task_id, logContext);
        return { success: true, message: `Task ${parameters.task_id} marked as completed` };

      case 'delete_todoist_task':
        await this.todoistService.deleteTask(parameters.task_id, logContext);
        return { success: true, message: `Task ${parameters.task_id} deleted permanently` };

      case 'get_completed_todoist_tasks':
        return await this.todoistService.getCompletedTasks({
          since: parameters.since,
          until: parameters.until,
          project_id: parameters.project_id,
          limit: parameters.limit,
          offset: parameters.offset,
        }, logContext);

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
    return [...TODOIST_TOOL_NAMES];
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
