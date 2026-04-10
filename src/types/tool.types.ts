// Interface for OpenAI function calls
export interface ToolCall {
  id: string;
  type: 'function';
  function: {
    name: string;
    arguments: string; // JSON string containing function parameters
  };
}

// Interface for tool execution results
export interface ToolResult {
  tool_call_id: string; // Maps back to the original tool call
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  content: any; // The actual result from the function
  error?: string; // Error message if execution failed
}

// Common interface for tool dispatchers
export interface ToolDispatcher {
  executeToolCalls(toolCalls: ToolCall[], userId: string): Promise<ToolResult[]>;
  isFunctionSupported(functionName: string): boolean;
}
