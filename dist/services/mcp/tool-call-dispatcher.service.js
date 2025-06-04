"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.ToolCallDispatcher = void 0;
class ToolCallDispatcher {
    constructor(mcpManager) {
        this.mcpManager = mcpManager;
    }
    /**
     * Executes multiple tool calls in parallel and returns results
     * @param toolCalls - Array of function calls from GPT
     * @param userId - User ID for context/authorization
     * @returns Promise<ToolResult[]> - Results of all tool executions
     */
    async executeToolCalls(toolCalls, userId) {
        // Execute all tool calls in parallel for better performance
        const promises = toolCalls.map((toolCall) => this.executeToolCall(toolCall, userId));
        const results = await Promise.allSettled(promises);
        // Convert Promise.allSettled results to our ToolResult format
        return results.map((result, index) => {
            var _a;
            const toolCallId = toolCalls[index].id;
            if (result.status === 'fulfilled') {
                return {
                    tool_call_id: toolCallId,
                    content: result.value,
                };
            }
            else {
                return {
                    tool_call_id: toolCallId,
                    content: null,
                    error: ((_a = result.reason) === null || _a === void 0 ? void 0 : _a.message) || 'Tool execution failed',
                };
            }
        });
    }
    /**
     * Executes a single tool call by routing to the appropriate MCP server
     * @param toolCall - The function call to execute
     * @param userId - User ID for context
     * @returns Promise<any> - The result of the function execution
     */
    async executeToolCall(toolCall, userId) {
        try {
            // Extract function details from the tool call
            const functionName = toolCall.function.name;
            const parameters = JSON.parse(toolCall.function.arguments);
            // Determine which MCP server should handle this function
            const mcpServerName = this.determineMCPServer(functionName);
            // Execute the function call on the appropriate MCP server
            return await this.mcpManager.executeToolCall(mcpServerName, functionName, parameters);
        }
        catch (error) {
            console.error(`Tool call execution error for ${toolCall.function.name}:`, error);
            throw error;
        }
    }
    /**
     * Routes function names to appropriate MCP servers
     * This is where you define which functions belong to which services
     * @param functionName - Name of the function to route
     * @returns string - Name of the MCP server that handles this function
     */
    determineMCPServer(functionName) {
        // Define which functions belong to Todoist
        const todoistFunctions = [
            'create_task',
            'get_tasks',
            'update_task',
            'complete_task',
            'get_projects',
        ];
        // Define which functions belong to Notion (for future expansion)
        const notionFunctions = ['create_page', 'update_page', 'search_pages'];
        // Route to appropriate server based on function name
        if (todoistFunctions.includes(functionName)) {
            return 'todoist';
        }
        else if (notionFunctions.includes(functionName)) {
            return 'notion';
        }
        // Throw error if function is not recognized
        throw new Error(`Unknown function: ${functionName}`);
    }
}
exports.ToolCallDispatcher = ToolCallDispatcher;
