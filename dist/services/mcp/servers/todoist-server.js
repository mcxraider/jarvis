"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.TodoistMCPServer = void 0;
const child_process_1 = require("child_process");
const events_1 = require("events");
class TodoistMCPServer extends events_1.EventEmitter {
    constructor(config) {
        super();
        this.process = null; // Child process running the MCP server
        this.isConnected = false; // Connection state
        this.pendingRequests = new Map(); // Track pending requests
        this.config = config;
    }
    /**
     * Starts the MCP server as a child process
     */
    async start() {
        try {
            // Spawn the MCP server process with environment variables
            this.process = (0, child_process_1.spawn)('node', [this.config.serverPath], {
                env: {
                    ...process.env,
                    TODOIST_API_KEY: this.config.apiKey, // Pass API key to child process
                },
                stdio: ['pipe', 'pipe', 'pipe'], // Enable stdin/stdout/stderr communication
            });
            this.setupProcessHandlers(); // Set up communication handlers
            await this.initialize(); // Initialize MCP protocol
            this.isConnected = true;
            console.log('Todoist MCP Server started successfully');
        }
        catch (error) {
            console.error('Failed to start Todoist MCP Server:', error);
            throw error;
        }
    }
    /**
     * Sets up handlers for process communication and lifecycle events
     */
    setupProcessHandlers() {
        var _a, _b;
        if (!this.process)
            return;
        (_a = this.process.stdout) === null || _a === void 0 ? void 0 : _a.on('data', (data) => {
            const messages = data.toString().split('\n').filter(Boolean);
            // Explicitly type 'msg' as string
            messages.forEach((msg) => this.handleMessage(msg));
        });
        // Handle error output from the MCP server
        (_b = this.process.stderr) === null || _b === void 0 ? void 0 : _b.on('data', (data) => {
            console.error('Todoist MCP Error:', data.toString());
        });
        // Handle process termination
        this.process.on('close', (code) => {
            console.log(`Todoist MCP Server exited with code ${code}`);
            this.isConnected = false;
            this.emit('disconnect'); // Notify listeners of disconnection
        });
    }
    /**
     * Handles incoming messages from the MCP server
     * @param message - Raw JSON message from the server
     */
    handleMessage(message) {
        try {
            const parsed = JSON.parse(message);
            // Check if this is a response to a pending request
            if (parsed.id && this.pendingRequests.has(parsed.id)) {
                const { resolve, reject } = this.pendingRequests.get(parsed.id);
                this.pendingRequests.delete(parsed.id);
                // Resolve or reject the promise based on the response
                if (parsed.error) {
                    reject(new Error(parsed.error.message));
                }
                else {
                    resolve(parsed.result);
                }
            }
        }
        catch (error) {
            console.error('Failed to parse MCP message:', error);
        }
    }
    /**
     * Initializes the MCP protocol with the server
     */
    async initialize() {
        await this.sendMessage({
            jsonrpc: '2.0',
            id: 'init',
            method: 'initialize',
            params: {
                protocolVersion: '2024-11-05', // MCP protocol version
                capabilities: { tools: {} }, // Declare our capabilities
                clientInfo: { name: 'telejarvis-backend', version: '1.0.0' },
            },
        });
    }
    /**
     * Executes a tool call on the MCP server
     * @param toolName - Name of the tool/function to call
     * @param parameters - Parameters to pass to the function
     * @returns Promise<any> - Result of the function execution
     */
    async executeToolCall(toolName, parameters) {
        return this.sendMessage({
            jsonrpc: '2.0',
            method: 'tools/call', // MCP method for calling tools
            params: {
                name: toolName,
                arguments: parameters,
            },
        });
    }
    /**
     * Sends a message to the MCP server and waits for response
     * @param message - The JSON-RPC message to send
     * @returns Promise<any> - The response from the server
     */
    async sendMessage(message) {
        return new Promise((resolve, reject) => {
            var _a;
            if (!this.process || !this.isConnected) {
                reject(new Error('MCP Server not connected'));
                return;
            }
            // Generate unique ID for this request
            const id = message.id || Date.now().toString();
            message.id = id;
            // Store promise handlers for when response arrives
            this.pendingRequests.set(id, { resolve, reject });
            // Send message to server via stdin
            (_a = this.process.stdin) === null || _a === void 0 ? void 0 : _a.write(JSON.stringify(message) + '\n');
            // Set timeout to prevent hanging requests
            setTimeout(() => {
                if (this.pendingRequests.has(id)) {
                    this.pendingRequests.delete(id);
                    reject(new Error('Request timeout'));
                }
            }, 30000); // 30 second timeout
        });
    }
    /**
     * Stops the MCP server process
     */
    async stop() {
        if (this.process) {
            this.process.kill();
            this.process = null;
            this.isConnected = false;
        }
    }
}
exports.TodoistMCPServer = TodoistMCPServer;
