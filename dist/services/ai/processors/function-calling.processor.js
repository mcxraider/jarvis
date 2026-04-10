"use strict";
/**
 * Function calling processor for GPT service
 *
 * @module FunctionCallingProcessor
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.FunctionCallingProcessor = void 0;
const logger_1 = require("../../../utils/logger");
const gpt_constants_1 = require("../constants/gpt.constants");
const gpt_prompts_1 = require("../../../types/gpt.prompts");
const todoist_tools_service_1 = require("../../mcp/servers/todoist/todoist-tools.service");
/**
 * Processor for handling GPT function calling capabilities
 */
class FunctionCallingProcessor {
    constructor(toolDispatcher) {
        this.toolDispatcher = toolDispatcher;
        this.toolsService = new todoist_tools_service_1.GPTToolsService(toolDispatcher);
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
    async processWithFunctionCalling(openai, model, temperature, message, userId) {
        var _a;
        const startTime = Date.now();
        try {
            // Send message to GPT with function calling capabilities enabled
            const response = await openai.chat.completions.create({
                model,
                messages: [
                    {
                        role: 'system',
                        content: (0, gpt_prompts_1.getFunctionCallingSystemPrompt)(),
                    },
                    {
                        role: 'user',
                        content: message,
                    },
                ],
                tools: this.toolsService.getAvailableTools(),
                tool_choice: 'auto', // Let GPT decide when to use functions
                max_tokens: gpt_constants_1.GPT_CONSTANTS.MAX_TOKENS,
                temperature,
            });
            const responseMessage = response.choices[0].message;
            // Log the full GPT response for inspection
            logger_1.logger.debug('GPT function calling response', {
                userId,
                hasToolCalls: !!(responseMessage.tool_calls && responseMessage.tool_calls.length > 0),
                toolCallsCount: ((_a = responseMessage.tool_calls) === null || _a === void 0 ? void 0 : _a.length) || 0,
                content: responseMessage.content,
            });
            // Check if GPT wants to call any functions
            if (responseMessage.tool_calls && responseMessage.tool_calls.length > 0) {
                const finalResponse = await this.handleToolCalls(responseMessage, openai, model, temperature, message, userId);
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
            const directResponse = responseMessage.content || "I apologize, but I couldn't process your request.";
            return {
                response: directResponse,
                originalMessage: message,
                processingTimeMs: Date.now() - startTime,
                usedFunctionCalling: false,
                functionCallsCount: 0,
                model,
            };
        }
        catch (error) {
            logger_1.logger.error('Function calling processing failed', {
                userId,
                error: error.message,
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
    async handleToolCalls(responseMessage, openai, model, temperature, originalMessage, userId) {
        if (!this.toolDispatcher || !responseMessage.tool_calls) {
            return "I'd like to help you with that, but I'm currently unable to execute the required actions.";
        }
        try {
            // Convert OpenAI tool calls to our internal format
            const toolCalls = responseMessage.tool_calls.map((toolCall) => ({
                id: toolCall.id,
                type: 'function',
                function: {
                    name: toolCall.function.name,
                    arguments: toolCall.function.arguments,
                },
            }));
            logger_1.logger.info('Executing tool calls', {
                userId,
                toolCalls: toolCalls.map((tc) => ({
                    id: tc.id,
                    name: tc.function.name,
                })),
            });
            // Filter out any function names the dispatcher does not support
            const supportedCalls = toolCalls.filter((tc) => {
                if (this.toolDispatcher.isFunctionSupported(tc.function.name))
                    return true;
                logger_1.logger.warn('Skipping unsupported function', { name: tc.function.name, userId });
                return false;
            });
            if (supportedCalls.length === 0) {
                return "I'm not able to perform that action right now.";
            }
            // Execute all supported tool calls
            const toolResults = await this.toolDispatcher.executeToolCalls(supportedCalls, userId);
            // Log execution results
            logger_1.logger.info('Tool execution completed', {
                userId,
                results: toolResults.map((result) => ({
                    tool_call_id: result.tool_call_id,
                    success: !result.error,
                    error: result.error,
                })),
            });
            // Generate final response based on tool execution results
            const finalResponse = await this.generateFinalResponse(openai, model, temperature, originalMessage, responseMessage, toolResults);
            return finalResponse;
        }
        catch (error) {
            logger_1.logger.error('Tool call execution failed', {
                userId,
                error: error.message,
            });
            return `I encountered an error while trying to help you: ${error.message}. Please try again or rephrase your request.`;
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
    async generateFinalResponse(openai, model, temperature, originalMessage, toolCallMessage, toolResults) {
        try {
            // Create messages array including the tool results
            const messages = [
                {
                    role: 'system',
                    content: gpt_prompts_1.FINAL_RESPONSE_PROMPT,
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
                max_tokens: gpt_constants_1.GPT_CONSTANTS.MAX_TOKENS,
                temperature,
            });
            return (finalResponse.choices[0].message.content ||
                'I completed the requested actions successfully.');
        }
        catch (error) {
            logger_1.logger.error('Failed to generate final response', {
                error: error.message,
            });
            // Fallback: create a simple response based on results
            const successfulActions = toolResults.filter((result) => !result.error);
            if (successfulActions.length > 0) {
                return `I successfully completed ${successfulActions.length} action(s) for you.`;
            }
            else {
                return 'I encountered some issues while processing your request. Please try again.';
            }
        }
    }
}
exports.FunctionCallingProcessor = FunctionCallingProcessor;
