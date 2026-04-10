/**
 * Function calling processor for GPT service
 *
 * @module FunctionCallingProcessor
 */

import OpenAI from 'openai';
import { logger } from '../../../utils/logger';
import { MessageProcessingResult } from '../../../types/gpt.types';
import { GPT_CONSTANTS } from '../constants/gpt.constants';
import { getFunctionCallingSystemPrompt, FINAL_RESPONSE_PROMPT } from '../../../types/gpt.prompts';
import { GPTToolsService } from '../../mcp/servers/todoist/todoist-tools.service';
import { ToolDispatcher, ToolCall } from '../../../types/mcp.types';

/**
 * Processor for handling GPT function calling capabilities
 */
export class FunctionCallingProcessor {
  private readonly toolsService: GPTToolsService;

  constructor(private readonly toolDispatcher?: ToolDispatcher) {
    this.toolsService = new GPTToolsService(toolDispatcher);
  }

  /**
   * Processes a message using GPT with function calling capabilities and executes tool calls
   *
   * @param openai - OpenAI client instance
   * @param model - Model to use for processing
   * @param temperature - Temperature setting for the model
   * @param message - The user's message to process
   * @param userId - The user identifier for context
   * @returns Promise<MessageProcessingResult> - The processing result with tool execution
   */
  async processWithFunctionCalling(
    openai: OpenAI,
    model: string,
    temperature: number,
    message: string,
    userId: string,
  ): Promise<MessageProcessingResult> {
    const startTime = Date.now();

    try {
      // Send message to GPT with function calling capabilities enabled
      const response = await openai.chat.completions.create({
        model,
        messages: [
          {
            role: 'system',
            content: getFunctionCallingSystemPrompt(),
          },
          {
            role: 'user',
            content: message,
          },
        ],
        tools: this.toolsService.getAvailableTools(),
        tool_choice: 'auto', // Let GPT decide when to use functions
        max_tokens: GPT_CONSTANTS.MAX_TOKENS,
        temperature,
      });

      const responseMessage = response.choices[0].message;

      // Log the full GPT response for inspection
      logger.debug('GPT function calling response', {
        userId,
        hasToolCalls: !!(responseMessage.tool_calls && responseMessage.tool_calls.length > 0),
        toolCallsCount: responseMessage.tool_calls?.length || 0,
        content: responseMessage.content,
      });

      // Check if GPT wants to call any functions
      if (responseMessage.tool_calls && responseMessage.tool_calls.length > 0) {
        const finalResponse = await this.handleToolCalls(
          responseMessage,
          openai,
          model,
          temperature,
          message,
          userId,
        );

        return {
          response: finalResponse,
          originalMessage: message,
          processingTimeMs: Date.now() - startTime,
          usedFunctionCalling: true,
          functionCallsCount: responseMessage.tool_calls.length,
          model,
        };
      }

      // If no function calls needed, return GPT's direct response
      const directResponse =
        responseMessage.content || "I apologize, but I couldn't process your request.";

      return {
        response: directResponse,
        originalMessage: message,
        processingTimeMs: Date.now() - startTime,
        usedFunctionCalling: false,
        functionCallsCount: 0,
        model,
      };
    } catch (error) {
      logger.error('Function calling processing failed', {
        userId,
        error: (error as Error).message,
      });

      throw error;
    }
  }

  /**
   * Handles tool calls by executing them and generating a final response
   *
   * @param responseMessage - The GPT response containing tool calls
   * @param openai - OpenAI client instance
   * @param model - Model to use for final response generation
   * @param temperature - Temperature setting
   * @param originalMessage - The user's original message
   * @param userId - User identifier
   * @returns Promise<string> - Final response to send to user
   */
  private async handleToolCalls(
    responseMessage: OpenAI.Chat.Completions.ChatCompletionMessage,
    openai: OpenAI,
    model: string,
    temperature: number,
    originalMessage: string,
    userId: string,
  ): Promise<string> {
    if (!this.toolDispatcher || !responseMessage.tool_calls) {
      return "I'd like to help you with that, but I'm currently unable to execute the required actions.";
    }

    try {
      // Convert OpenAI tool calls to our internal format
      const toolCalls: ToolCall[] = responseMessage.tool_calls.map((toolCall) => ({
        id: toolCall.id,
        type: 'function',
        function: {
          name: toolCall.function.name,
          arguments: toolCall.function.arguments,
        },
      }));

      logger.info('Executing tool calls', {
        userId,
        toolCalls: toolCalls.map((tc) => ({
          id: tc.id,
          name: tc.function.name,
        })),
      });

      // Filter out any function names the dispatcher does not support
      const supportedCalls = toolCalls.filter((tc) => {
        if (this.toolDispatcher!.isFunctionSupported(tc.function.name)) return true;
        logger.warn('Skipping unsupported function', { name: tc.function.name, userId });
        return false;
      });

      if (supportedCalls.length === 0) {
        return "I'm not able to perform that action right now.";
      }

      // Execute all supported tool calls
      const toolResults = await this.toolDispatcher.executeToolCalls(supportedCalls, userId);

      // Log execution results
      logger.info('Tool execution completed', {
        userId,
        results: toolResults.map((result) => ({
          tool_call_id: result.tool_call_id,
          success: !result.error,
          error: result.error,
        })),
      });

      // Generate final response based on tool execution results
      const finalResponse = await this.generateFinalResponse(
        openai,
        model,
        temperature,
        originalMessage,
        responseMessage,
        toolResults,
      );

      return finalResponse;
    } catch (error) {
      logger.error('Tool call execution failed', {
        userId,
        error: (error as Error).message,
      });

      return `I encountered an error while trying to help you: ${(error as Error).message}. Please try again or rephrase your request.`;
    }
  }

  /**
   * Generates a final natural language response based on tool execution results
   *
   * @param openai - OpenAI client instance
   * @param model - Model to use
   * @param temperature - Temperature setting
   * @param originalMessage - User's original message
   * @param toolCallMessage - GPT's tool call message
   * @param toolResults - Results from tool execution
   * @returns Promise<string> - Final response
   */
  private async generateFinalResponse(
    openai: OpenAI,
    model: string,
    temperature: number,
    originalMessage: string,
    toolCallMessage: OpenAI.Chat.Completions.ChatCompletionMessage,
    toolResults: any[],
  ): Promise<string> {
    try {
      // Create messages array including the tool results
      const messages: OpenAI.Chat.Completions.ChatCompletionMessageParam[] = [
        {
          role: 'system',
          content: FINAL_RESPONSE_PROMPT,
        },
        {
          role: 'user',
          content: originalMessage,
        },
        {
          role: 'assistant',
          content: toolCallMessage.content || null,
          tool_calls: toolCallMessage.tool_calls,
        },
      ];

      // Add tool results as tool messages
      toolResults.forEach((result) => {
        messages.push({
          role: 'tool',
          tool_call_id: result.tool_call_id,
          content: JSON.stringify(result.content),
        });
      });

      const finalResponse = await openai.chat.completions.create({
        model,
        messages,
        max_tokens: GPT_CONSTANTS.MAX_TOKENS,
        temperature,
      });

      return (
        finalResponse.choices[0].message.content ||
        'I completed the requested actions successfully.'
      );
    } catch (error) {
      logger.error('Failed to generate final response', {
        error: (error as Error).message,
      });

      // Fallback: create a simple response based on results
      const successfulActions = toolResults.filter((result) => !result.error);
      if (successfulActions.length > 0) {
        return `I successfully completed ${successfulActions.length} action(s) for you.`;
      } else {
        return 'I encountered some issues while processing your request. Please try again.';
      }
    }
  }
}
