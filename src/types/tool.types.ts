import { LogContext } from '../utils/logger';

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
  toolName?: string; // Function/tool name used for deterministic reporting
  displayLabel?: string; // Optional human-readable label for reporting
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  content: any; // The actual result from the function
  error?: string; // Error message if execution failed
}

// Common interface for tool dispatchers
export interface ToolDispatcher {
  executeToolCalls(
    toolCalls: ToolCall[],
    userId: string,
    logContext?: LogContext,
  ): Promise<ToolResult[]>;
  isFunctionSupported(functionName: string): boolean;
}
