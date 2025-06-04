//src/config/mcp.config.ts
import * as dotenv from 'dotenv';
dotenv.config(); // This loads variables from .env into process.env

import { MCPServerConfig } from '../types/mcp.types';

export const mcpConfig: MCPServerConfig[] = [
  {
    name: 'todoist',
    type: 'todoist',
    serverPath: '../todoist-mcp/build/index.js',
    apiKey: process.env.TODOIST_API_KEY || '',
    enabled: true,
  },
  // Add more MCP server configurations here as needed
];
