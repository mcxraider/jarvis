"use strict";
// src/services/ai/gpt.service.ts
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.GPTService = void 0;
/**
 * Service for generating text content using OpenAI GPT models with function calling capabilities.
 * Handles intelligent function calling, text processing, and response formatting.
 *
 * @example
 * ```typescript
 * const gptService = new GPTService(toolDispatcher);
 * const response = await gptService.processMessage('Create a task to buy groceries', 'user123');
 * ```
 */
const openai_1 = __importDefault(require("openai"));
const logger_1 = require("../../utils/logger");
const textValidation_1 = require("../../utils/ai/textValidation");
const poemGenerator_1 = require("../../utils/ai/poemGenerator");
/**
 * Constants for GPT service configuration
 */
const GPT_CONSTANTS = {
    /** Default GPT model for text generation */
    DEFAULT_MODEL: 'gpt-4o',
    /** Maximum input text length for processing */
    MAX_INPUT_LENGTH: 4000,
    /** Target poem length in words */
    TARGET_POEM_LENGTH: 30,
    /** Maximum response tokens */
    MAX_TOKENS: 1000,
    /** Temperature for creativity (0.0 to 2.0) */
    TEMPERATURE: 0.7,
    /** Maximum retries for API calls */
    MAX_RETRIES: 3,
    /** Function calling timeout */
    FUNCTION_CALL_TIMEOUT_MS: 30000,
};
/**
 * Service for generating text content using OpenAI GPT models with function calling
 */
class GPTService {
    /**
     * Creates a new GPTService instance
     *
     * @param toolDispatcher - Optional tool dispatcher for function calling
     * @param config - Configuration options for the service
     * @throws {Error} If OpenAI API key is not provided
     */
    constructor(toolDispatcher, config) {
        const apiKey = (config === null || config === void 0 ? void 0 : config.apiKey) || process.env.OPENAI_API_KEY;
        if (!apiKey) {
            throw new Error('OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it in config.');
        }
        this.openai = new openai_1.default({ apiKey });
        this.toolDispatcher = toolDispatcher;
        this.enableFunctionCalling = (config === null || config === void 0 ? void 0 : config.enableFunctionCalling) !== false && !!toolDispatcher;
        // Set default configuration with provided overrides
        this.config = {
            apiKey,
            model: (config === null || config === void 0 ? void 0 : config.model) || GPT_CONSTANTS.DEFAULT_MODEL,
            maxInputLength: (config === null || config === void 0 ? void 0 : config.maxInputLength) || GPT_CONSTANTS.MAX_INPUT_LENGTH,
            targetPoemLength: (config === null || config === void 0 ? void 0 : config.targetPoemLength) || GPT_CONSTANTS.TARGET_POEM_LENGTH,
            temperature: (config === null || config === void 0 ? void 0 : config.temperature) || GPT_CONSTANTS.TEMPERATURE,
        };
        this.poemGenerator = new poemGenerator_1.PoemGenerator(this.config.targetPoemLength);
        logger_1.logger.info('GPTService initialized', {
            model: this.config.model,
            maxInputLength: this.config.maxInputLength,
            targetPoemLength: this.config.targetPoemLength,
            temperature: this.config.temperature,
            functionCallingEnabled: this.enableFunctionCalling,
            hasToolDispatcher: !!this.toolDispatcher,
        });
    }
    /**
     * Main method to process user messages with intelligent function calling
     * @param message - The user's natural language message
     * @param userId - User identifier for context/authorization
     * @returns Promise<string> - The final response to send back to user
     */
    async processMessage(message, userId) {
        const startTime = Date.now();
        logger_1.logger.info('Processing message with GPT', {
            userId,
            messageLength: message.length,
            functionCallingEnabled: this.enableFunctionCalling,
        });
        try {
            // Validate input message
            this.validateInputMessage(message);
            // Process with function calling if enabled
            if (this.enableFunctionCalling && this.toolDispatcher) {
                const result = await this.processMessageWithFunctionCalling(message, userId || 'anonymous');
                return result.response;
            }
            // Fallback to simple text generation
            return await this.processMessageSimple(message, userId);
        }
        catch (error) {
            const processingTimeMs = Date.now() - startTime;
            logger_1.logger.error('Message processing failed', {
                userId,
                messageLength: message.length,
                error: error.message,
                processingTimeMs,
            });
            return this.handleProcessingError(error);
        }
    }
    /**
     * Process message with function calling capabilities
     * @param message - User message
     * @param userId - User ID
     * @returns Promise<MessageProcessingResult> - Processing result
     * @private
     */
    async processMessageWithFunctionCalling(message, userId) {
        const startTime = Date.now();
        try {
            // Send message to GPT with function calling capabilities enabled
            const response = await this.openai.chat.completions.create({
                model: this.config.model,
                messages: [
                    {
                        role: 'system',
                        content: this.getSystemPromptForFunctionCalling(),
                    },
                    {
                        role: 'user',
                        content: message,
                    },
                ],
                tools: this.getAvailableTools(),
                tool_choice: 'auto', // Let GPT decide when to use functions
                max_tokens: GPT_CONSTANTS.MAX_TOKENS,
                temperature: this.config.temperature,
            });
            const responseMessage = response.choices[0].message;
            // Check if GPT wants to call any functions
            if (responseMessage.tool_calls && responseMessage.tool_calls.length > 0) {
                const finalResponse = await this.handleToolCalls(responseMessage, message, userId);
                return {
                    response: finalResponse,
                    originalMessage: message,
                    processingTimeMs: Date.now() - startTime,
                    usedFunctionCalling: true,
                    functionCallsCount: responseMessage.tool_calls.length,
                    model: this.config.model,
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
                model: this.config.model,
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
     * Process message with simple text generation (no function calling)
     * @param message - User message
     * @param userId - User ID
     * @returns Promise<string> - Generated response
     * @private
     */
    async processMessageSimple(message, userId) {
        var _a, _b;
        try {
            const completion = await this.openai.chat.completions.create({
                model: this.config.model,
                messages: [
                    {
                        role: 'system',
                        content: 'You are a helpful and friendly assistant. Provide clear, concise, and helpful responses.',
                    },
                    {
                        role: 'user',
                        content: message,
                    },
                ],
                max_tokens: GPT_CONSTANTS.MAX_TOKENS,
                temperature: this.config.temperature,
            });
            return (((_b = (_a = completion.choices[0]) === null || _a === void 0 ? void 0 : _a.message) === null || _b === void 0 ? void 0 : _b.content) ||
                "I apologize, but I couldn't generate a response.");
        }
        catch (error) {
            logger_1.logger.error('Simple message processing failed', {
                userId,
                error: error.message,
            });
            throw error;
        }
    }
    /**
     * Validates the input message for Response
     *
     * @param message - The message to validate
     * @throws {Error} If message is invalid
     * @private
     */
    validateInputMessage(message) {
        if (!message || message.trim().length === 0) {
            throw new Error('Message cannot be empty');
        }
        (0, textValidation_1.validateTextLength)(message, this.config.maxInputLength);
    }
    /**
     * Performs the actual Response using OpenAI GPT
     *
     * @param message - The message to create a poem about
     * @returns Promise resolving to the generated poem
     * @private
     */
    async performPoemGeneration(message) {
        var _a, _b, _c;
        let lastError = null;
        for (let attempt = 1; attempt <= GPT_CONSTANTS.MAX_RETRIES; attempt++) {
            try {
                const prompt = this.poemGenerator.createPoemPrompt(message);
                const completion = await this.openai.chat.completions.create({
                    model: this.config.model,
                    messages: [
                        {
                            role: 'system',
                            content: this.poemGenerator.getSystemPrompt(),
                        },
                        {
                            role: 'user',
                            content: prompt,
                        },
                    ],
                    max_tokens: GPT_CONSTANTS.MAX_TOKENS,
                    temperature: this.config.temperature,
                    presence_penalty: 0.6, // Encourage diverse vocabulary
                    frequency_penalty: 0.3, // Reduce repetition
                });
                const poem = (_c = (_b = (_a = completion.choices[0]) === null || _a === void 0 ? void 0 : _a.message) === null || _b === void 0 ? void 0 : _b.content) === null || _c === void 0 ? void 0 : _c.trim();
                if (!poem) {
                    throw new Error('No poem content generated');
                }
                // Validate and potentially clean up the poem
                return this.poemGenerator.validateAndCleanPoem(poem);
            }
            catch (error) {
                lastError = error;
                logger_1.logger.warn(`Response attempt ${attempt} failed`, {
                    attempt,
                    error: lastError.message,
                });
                if (attempt === GPT_CONSTANTS.MAX_RETRIES) {
                    break;
                }
                // Wait before retrying (exponential backoff)
                await this.delay(Math.pow(2, attempt) * 1000);
            }
        }
        // If all retries failed, handle specific error types
        if (lastError) {
            if (lastError.message.includes('rate limit')) {
                throw new Error('Service is temporarily busy. Please try again in a moment.');
            }
            if (lastError.message.includes('context_length')) {
                throw new Error('Message is too complex for Response.');
            }
        }
        throw new Error(`Failed to generate poem after ${GPT_CONSTANTS.MAX_RETRIES} attempts`);
    }
    /**
     * Counts words in a given text
     *
     * @param text - The text to count words in
     * @returns Number of words
     * @private
     */
    countWords(text) {
        return text
            .trim()
            .split(/\s+/)
            .filter((word) => word.length > 0).length;
    }
    /**
     * Delays execution for the specified number of milliseconds
     *
     * @param ms - Milliseconds to delay
     * @returns Promise that resolves after the delay
     * @private
     */
    delay(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }
    /**
     * Handle processing errors and return user-friendly error messages
     * @param error - The error that occurred during processing
     * @returns User-friendly error message
     * @private
     */
    handleProcessingError(error) {
        logger_1.logger.error('GPT processing error', { error: error.message });
        // Handle specific error types with user-friendly messages
        if (error.message.includes('rate limit')) {
            return "I'm currently experiencing high demand. Please try again in a moment.";
        }
        if (error.message.includes('context_length') || error.message.includes('token')) {
            return 'Your message is too long for me to process. Please try a shorter message.';
        }
        if (error.message.includes('timeout')) {
            return 'The request took too long to process. Please try again.';
        }
        if (error.message.includes('API key')) {
            return "I'm experiencing configuration issues. Please contact support.";
        }
        // Generic error message for unknown errors
        return 'I encountered an issue processing your request. Please try again or rephrase your message.';
    }
    /**
     * Generate system prompt for function calling mode
     * @returns System prompt string for GPT function calling
     * @private
     */
    getSystemPromptForFunctionCalling() {
        return `You are Jarvis, an intelligent assistant with access to various tools and functions. Your goal is to help users by:

1. Understanding their natural language requests
2. Deciding which tools or functions to use (if any) to fulfill their requests
3. Calling the appropriate functions with correct parameters
4. Providing helpful, clear responses based on the results

Available capabilities include:
- Task management (creating, updating, listing tasks)
- Audio processing and transcription
- General assistance and conversation
- File operations and utilities

Guidelines:
- Use tools when they can help accomplish the user's request
- If no tools are needed, respond conversationally
- Always be helpful, clear, and concise
- Explain what you're doing when using tools
- Handle errors gracefully and suggest alternatives

Current date: ${new Date().toLocaleDateString()}`;
    }
    /**
     * Get available tools/functions for OpenAI function calling
     * @returns Array of tool definitions for OpenAI
     * @private
     */
    getAvailableTools() {
        if (!this.toolDispatcher) {
            return [];
        }
        // Define core tools available through the tool dispatcher
        return [
            {
                type: 'function',
                function: {
                    name: 'create_task',
                    description: 'Create a new task or todo item in Todoist',
                    parameters: {
                        type: 'object',
                        properties: {
                            content: {
                                type: 'string',
                                description: 'The title or content of the task',
                            },
                            description: {
                                type: 'string',
                                description: 'Optional detailed description of the task',
                            },
                            due_string: {
                                type: 'string',
                                description: 'Optional due date in natural language (e.g., "tomorrow", "next week")',
                            },
                            priority: {
                                type: 'number',
                                enum: [1, 2, 3, 4],
                                description: 'Task priority level (1=normal, 2=high, 3=very high, 4=urgent)',
                            },
                            project_id: {
                                type: 'string',
                                description: 'Optional project ID to assign the task to',
                            },
                        },
                        required: ['content'],
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
                            filter: {
                                type: 'string',
                                description: 'Todoist filter expression (e.g., "today", "overdue")',
                            },
                            limit: {
                                type: 'number',
                                description: 'Maximum number of tasks to return (default: 50)',
                            },
                        },
                    },
                },
            },
            {
                type: 'function',
                function: {
                    name: 'update_task',
                    description: 'Update an existing task in Todoist',
                    parameters: {
                        type: 'object',
                        properties: {
                            id: {
                                type: 'string',
                                description: 'The ID of the task to update',
                            },
                            content: {
                                type: 'string',
                                description: 'New content/title for the task',
                            },
                            description: {
                                type: 'string',
                                description: 'New description for the task',
                            },
                            due_string: {
                                type: 'string',
                                description: 'New due date in natural language',
                            },
                            priority: {
                                type: 'number',
                                enum: [1, 2, 3, 4],
                                description: 'New priority level',
                            },
                        },
                        required: ['id'],
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
                            id: {
                                type: 'string',
                                description: 'The ID of the task to complete',
                            },
                        },
                        required: ['id'],
                    },
                },
            },
            {
                type: 'function',
                function: {
                    name: 'get_projects',
                    description: 'Get all projects from Todoist',
                    parameters: {
                        type: 'object',
                        properties: {},
                    },
                },
            },
            {
                type: 'function',
                function: {
                    name: 'generate_poem',
                    description: 'Generate a creative poem about a given topic or message',
                    parameters: {
                        type: 'object',
                        properties: {
                            topic: {
                                type: 'string',
                                description: 'The topic or message to create a poem about',
                            },
                            style: {
                                type: 'string',
                                enum: ['free_verse', 'rhyming', 'haiku', 'limerick'],
                                description: 'The style of poem to generate',
                            },
                        },
                        required: ['topic'],
                    },
                },
            },
        ];
    }
    /**
     * Handle tool calls from GPT and execute them through the tool dispatcher
     * @param responseMessage - GPT response containing tool calls
     * @param originalMessage - Original user message
     * @param userId - User ID for context
     * @returns Promise<string> - Final response after executing tool calls
     * @private
     */
    async handleToolCalls(responseMessage, originalMessage, userId) {
        if (!responseMessage.tool_calls || !this.toolDispatcher) {
            return 'I tried to use a tool, but something went wrong. Please try again.';
        }
        try {
            logger_1.logger.info('Executing tool calls', {
                userId,
                toolCallsCount: responseMessage.tool_calls.length,
                toolNames: responseMessage.tool_calls.map((tc) => tc.function.name),
            });
            // Convert OpenAI tool calls to our ToolCall format
            const toolCalls = responseMessage.tool_calls.map((toolCall) => ({
                id: toolCall.id,
                type: 'function',
                function: {
                    name: toolCall.function.name,
                    arguments: toolCall.function.arguments,
                },
            }));
            // Handle special cases that don't go through MCP
            const mcpToolCalls = [];
            const localResults = [];
            for (const toolCall of toolCalls) {
                if (toolCall.function.name === 'generate_poem') {
                    // Handle poem generation locally
                    try {
                        const parameters = JSON.parse(toolCall.function.arguments);
                        const poemResult = await this.generatePoemTool(parameters.topic, parameters.style);
                        localResults.push({
                            tool_call_id: toolCall.id,
                            content: poemResult,
                        });
                    }
                    catch (error) {
                        localResults.push({
                            tool_call_id: toolCall.id,
                            content: null,
                            error: `Failed to generate poem: ${error.message}`,
                        });
                    }
                }
                else {
                    // Route to MCP dispatcher
                    mcpToolCalls.push(toolCall);
                }
            }
            // Execute MCP tool calls if any
            let mcpResults = [];
            if (mcpToolCalls.length > 0) {
                mcpResults = await this.toolDispatcher.executeToolCalls(mcpToolCalls, userId);
            }
            // Combine all results
            const allResults = [...localResults, ...mcpResults];
            // Generate final response based on tool results
            return await this.generateFinalResponse(originalMessage, allResults, userId);
        }
        catch (error) {
            logger_1.logger.error('Tool calls execution failed', {
                userId,
                error: error.message,
            });
            return `I encountered an error while executing your request: ${error.message}. Please try again.`;
        }
    }
    /**
     * Generate a poem using the legacy poem generation functionality
     * @param topic - Topic for the poem
     * @param style - Style of the poem (optional)
     * @returns Promise<string> - Generated poem
     * @private
     */
    async generatePoemTool(topic, style) {
        try {
            const poemResult = await this.generatePoem(topic);
            return poemResult.poem;
        }
        catch (error) {
            logger_1.logger.error('Poem generation tool failed', { error: error.message });
            return `I couldn't generate a poem about "${topic}". ${error.message}`;
        }
    }
    /**
     * Generate final response based on tool execution results
     * @param originalMessage - Original user message
     * @param toolResults - Results from tool executions
     * @param userId - User ID
     * @returns Promise<string> - Final formatted response
     * @private
     */
    async generateFinalResponse(originalMessage, toolResults, userId) {
        var _a, _b;
        try {
            // Create context for GPT to generate a natural response
            const toolSummary = toolResults
                .map((result) => {
                const success = !result.error;
                const content = result.error || result.content;
                return `Tool Call ID: ${result.tool_call_id}\nSuccess: ${success}\nResult: ${JSON.stringify(content, null, 2)}`;
            })
                .join('\n\n');
            const completion = await this.openai.chat.completions.create({
                model: this.config.model,
                messages: [
                    {
                        role: 'system',
                        content: `You are Jarvis, an intelligent assistant. Based on the tool execution results below, provide a natural, helpful response to the user. Be conversational and explain what was accomplished.`,
                    },
                    {
                        role: 'user',
                        content: `Original request: "${originalMessage}"\n\nTool execution results:\n${toolSummary}\n\nPlease provide a natural response summarizing what was accomplished.`,
                    },
                ],
                max_tokens: GPT_CONSTANTS.MAX_TOKENS,
                temperature: 0.7,
            });
            return (((_b = (_a = completion.choices[0]) === null || _a === void 0 ? void 0 : _a.message) === null || _b === void 0 ? void 0 : _b.content) ||
                'I completed your request, but had trouble generating a summary.');
        }
        catch (error) {
            logger_1.logger.error('Failed to generate final response', {
                userId,
                error: error.message,
            });
            // Fallback: create a simple summary
            const successCount = toolResults.filter((r) => !r.error).length;
            const totalCount = toolResults.length;
            return `I executed ${successCount} out of ${totalCount} requested actions. ${successCount > 0 ? 'Some operations completed successfully.' : 'Unfortunately, the operations encountered issues. Please try again.'}`;
        }
    }
    /**
     * Legacy poem generation method for backward compatibility
     * @param message - The message to create a poem about
     * @returns Promise<PoemGenerationResult> - The generation result
     */
    async generatePoem(message) {
        const startTime = Date.now();
        logger_1.logger.info('Generating poem', {
            messageLength: message.length,
            model: this.config.model,
        });
        try {
            this.validateInputMessage(message);
            const poem = await this.performPoemGeneration(message);
            const wordCount = this.countWords(poem);
            const processingTimeMs = Date.now() - startTime;
            logger_1.logger.info('Poem generation completed', {
                wordCount,
                processingTimeMs,
                model: this.config.model,
            });
            return {
                poem,
                originalMessage: message,
                processingTimeMs,
                wordCount,
                model: this.config.model,
            };
        }
        catch (error) {
            const processingTimeMs = Date.now() - startTime;
            logger_1.logger.error('Poem generation failed', {
                error: error.message,
                processingTimeMs,
                messageLength: message.length,
            });
            throw error;
        }
    }
    /**
     * Gets current service configuration
     *
     * @returns Service configuration (excluding sensitive data)
     */
    getConfig() {
        return {
            model: this.config.model,
            maxInputLength: this.config.maxInputLength,
            targetPoemLength: this.config.targetPoemLength,
            temperature: this.config.temperature,
            enableFunctionCalling: this.enableFunctionCalling,
        };
    }
}
exports.GPTService = GPTService;
