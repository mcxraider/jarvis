/**
 * Tool definitions for OpenAI function calling
 *
 * @module GPTTools
 */

import OpenAI from 'openai';
import { ToolDispatcher } from '../../types/tool.types';
import { TODOIST_TOOL_DEFINITIONS } from './todoist-tool-schemas';

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

    return TODOIST_TOOL_DEFINITIONS;
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
