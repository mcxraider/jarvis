import { MCPManagerService } from './mcp-manager.service';
import { ToolCall, ToolResult } from '../../types/mcp.types';

export class ToolCallDispatcher {
  constructor(private mcpManager: MCPManagerService) {}

  /**
   * Executes multiple tool calls in parallel and returns results
   * @param toolCalls - Array of function calls from GPT
   * @param userId - User ID for context/authorization
   * @returns Promise<ToolResult[]> - Results of all tool executions
   */
  async executeToolCalls(toolCalls: ToolCall[], userId: string): Promise<ToolResult[]> {
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
   * Executes a single tool call by routing to the appropriate MCP server
   * @param toolCall - The function call to execute
   * @param userId - User ID for context
   * @returns Promise<any> - The result of the function execution
   */
  private async executeToolCall(toolCall: ToolCall, userId: string): Promise<any> {
    try {
      // Extract function details from the tool call
      const functionName = toolCall.function.name;
      const parameters = JSON.parse(toolCall.function.arguments);

      // Determine which MCP server should handle this function
      const mcpServerName = this.determineMCPServer(functionName);

      // Execute the function call on the appropriate MCP server
      return await this.mcpManager.executeToolCall(mcpServerName, functionName, parameters);
    } catch (error) {
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
  private determineMCPServer(functionName: string): string {
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
    } else if (notionFunctions.includes(functionName)) {
      return 'notion';
    }

    // Throw error if function is not recognized
    throw new Error(`Unknown function: ${functionName}`);
  }
}
