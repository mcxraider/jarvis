"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.mcpConfig = void 0;
exports.mcpConfig = [
    {
        name: 'todoist',
        type: 'todoist',
        serverPath: './mcp-servers/todoist/index.js',
        apiKey: process.env.TODOIST_API_KEY || '',
        enabled: true,
    },
    // Add more MCP server configurations here as needed
];
