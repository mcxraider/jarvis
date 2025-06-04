"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MCPManagerService = void 0;
const todoist_server_1 = require("./servers/todoist-server");
class MCPManagerService {
    constructor() {
        this.servers = new Map(); // Registry of active MCP servers
    }
    /**
     * Initializes all configured MCP servers
     * @param configs - Array of server configurations
     */
    async initializeServers(configs) {
        for (const config of configs) {
            try {
                let server;
                switch (config.type) {
                    case 'todoist':
                        // Type assertion: Tell TypeScript that 'config' is now specifically a TodoistMCPConfig
                        server = new todoist_server_1.TodoistMCPServer(config);
                        break;
                    default:
                        console.warn(`Unknown MCP server type: ${config.type}`);
                        continue;
                }
                // Start the server and register it
                await server.start();
                this.servers.set(config.name, server);
                console.log(`MCP Server ${config.name} initialized successfully`);
            }
            catch (error) {
                console.error(`Failed to initialize MCP server ${config.name}:`, error);
            }
        }
    }
    /**
     * Executes a tool call on a specific MCP server
     * @param serverName - Name of the server to use
     * @param toolName - Name of the tool/function to call
     * @param parameters - Parameters for the function
     * @returns Promise<any> - Result of the function execution
     */
    async executeToolCall(serverName, toolName, parameters) {
        const server = this.servers.get(serverName);
        if (!server) {
            throw new Error(`MCP Server ${serverName} not found`);
        }
        return server.executeToolCall(toolName, parameters);
    }
    /**
     * Shuts down all MCP servers gracefully
     */
    async shutdown() {
        for (const [name, server] of this.servers) {
            try {
                await server.stop();
                console.log(`MCP Server ${name} stopped`);
            }
            catch (error) {
                console.error(`Error stopping MCP server ${name}:`, error);
            }
        }
        this.servers.clear();
    }
}
exports.MCPManagerService = MCPManagerService;
