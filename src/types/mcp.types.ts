// JSON-RPC 2.0 message format for MCP communication
export interface MCPMessage {
  jsonrpc: '2.0';
  id?: string;
  method?: string;
  params?: any;
  result?: any;
  error?: {
    code: number;
    message: string;
    data?: any;
  };
}

// Base configuration for MCP servers
export interface MCPServerConfig {
  name: string; // Unique identifier for the server
  type: 'todoist' | 'notion' | 'apple-notes'; // Server type
  serverPath: string; // Path to the MCP server executable
  apiKey: string; // API key for the external service
  enabled: boolean; // Whether this server should be started
}

// Specific configuration for Todoist MCP server
export interface TodoistMCPConfig extends MCPServerConfig {
  type: 'todoist';
}

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
  content: any; // The actual result from the function
  error?: string; // Error message if execution failed
}
