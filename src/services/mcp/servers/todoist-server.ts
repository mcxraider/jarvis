// src/services/mcp/servers/todoist-server.ts
import { spawn, ChildProcess } from 'child_process';
import { EventEmitter } from 'events';
import { TodoistMCPConfig, MCPMessage } from '../../../types/mcp.types';

export class TodoistMCPServer extends EventEmitter {
  private process: ChildProcess | null = null;
  private config: TodoistMCPConfig;
  private isConnected = false;
  private pendingRequests = new Map<string, { resolve: Function; reject: Function }>();

  constructor(config: TodoistMCPConfig) {
    super();
    this.config = config;
  }

  /**
   * Starts the MCP server as a child process
   */
  async start(): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.process && this.isConnected) {
        resolve(); // Already connected
        return;
      }

      let stdoutBuffer = ''; // Buffer for stdout
      let stderrBuffer = ''; // Buffer for stderr

      // Flag to ensure the outer promise is resolved/rejected only once
      let startPromiseCompleted = false;
      const completeStartPromise = (err?: Error) => {
        if (!startPromiseCompleted) {
          startPromiseCompleted = true;
          if (err) {
            reject(err);
          } else {
            resolve();
          }
        }
      };

      // Function to check for ready state and trigger initialization
      // This is now called from both stdout and stderr listeners
      const checkReadyAndInitialize = (output: string, source: 'stdout' | 'stderr') => {
        if (output.includes('Todoist Agent Server running on stdio')) {
          if (!this.isConnected) {
            // Only proceed if not already connected
            console.log(`Todoist MCP Server reports ready via ${source}.`);
            this.isConnected = true; // Set connected state

            // IMPORTANT: Remove the initial data listeners and attach the actual JSON-RPC handler
            // This prevents the 'ready' message (and any other initial non-JSON logs) from being parsed as JSON
            this.process?.stdout?.removeAllListeners('data');
            this.process?.stderr?.removeAllListeners('data'); // Remove initial stderr handler too

            // Re-attach the main message handler to stdout
            this.process?.stdout?.on('data', (d) => {
              const messages = d.toString().split('\n').filter(Boolean);
              messages.forEach((msg: string) => this.handleMessage(msg));
            });
            // Re-attach a basic stderr listener for actual errors
            this.process?.stderr?.on('data', (d) => {
              console.error('Todoist MCP Server runtime error:', d.toString().trim());
            });

            // Now that it's connected, proceed with initialization
            this.initialize()
              .then(() => {
                console.log('Todoist MCP Server started and initialized successfully');
                completeStartPromise(); // Resolve the outer promise
              })
              .catch((err) => {
                console.error('Failed to initialize Todoist MCP Server after connection:', err);
                completeStartPromise(err); // Reject if initialization fails
              });
          }
          return true; // Indicate that ready message was found
        }
        return false; // Indicate that ready message was not found
      };

      try {
        this.process = spawn('node', [this.config.serverPath], {
          env: {
            ...process.env,
            TODOIST_API_KEY: this.config.apiKey,
          },
          stdio: ['pipe', 'pipe', 'pipe'],
        });

        // Set up initial stdout data handler for buffering and ready check
        this.process.stdout?.on('data', (data) => {
          const chunk = data.toString();
          stdoutBuffer += chunk;
          // Check for ready message
          if (checkReadyAndInitialize(stdoutBuffer, 'stdout')) {
            stdoutBuffer = ''; // Clear buffer once processed
            return;
          }
          // If not ready yet and not the ready message, buffer continues
          // For now, if we are here and not connected, the buffer accumulates.
          // Once connected, the listener will be replaced.
        });

        // Set up stderr data handler for buffering and ready check
        this.process.stderr?.on('data', (data) => {
          const chunk = data.toString();
          stderrBuffer += chunk; // Accumulate stderr output
          console.error('Todoist MCP Server stderr (initial):', chunk.trim()); // Always log stderr for debugging

          // Check if stderr contains the "ready" signal
          if (checkReadyAndInitialize(stderrBuffer, 'stderr')) {
            stderrBuffer = ''; // Clear buffer if ready message was found
            return; // Don't reject if this is the "ready" message
          }

          // If not connected yet AND the stderr output is NOT the "ready" signal, then it's a true error
          if (!this.isConnected) {
            // Reject the start promise with the stderr content as the error message
            completeStartPromise(
              new Error(`Todoist MCP Server failed to start: ${stderrBuffer.trim()}`),
            );
          }
        });

        // Handle process termination (e.g., if it crashes before connecting)
        this.process.on('close', (code) => {
          console.log(`Todoist MCP Server exited with code ${code}`);
          this.isConnected = false;
          this.emit('disconnect');

          // If the process closed before we successfully connected and initialized
          if (code !== 0) {
            // If it exited with a non-zero code
            // Only reject if the start promise hasn't already completed
            completeStartPromise(
              new Error(`Todoist MCP Server exited unexpectedly with code ${code}`),
            );
          }
        });

        // Handle general process errors (e.g., executable not found, permissions)
        this.process.on('error', (err) => {
          console.error('Failed to spawn Todoist MCP Server process:', err);
          completeStartPromise(err);
        });
      } catch (error) {
        console.error('Failed to spawn Todoist MCP Server process (initial error):', error);
        completeStartPromise(error as Error);
      }
    });
  }

  /**
   * Handles incoming messages from the MCP server
   * @param message - Raw JSON message from the server
   */
  private handleMessage(message: string): void {
    try {
      const parsed = JSON.parse(message) as MCPMessage;

      // Check if this is a response to a pending request
      if (parsed.id && this.pendingRequests.has(parsed.id)) {
        const { resolve, reject } = this.pendingRequests.get(parsed.id)!;
        this.pendingRequests.delete(parsed.id);

        // Resolve or reject the promise based on the response
        if (parsed.error) {
          reject(new Error(parsed.error.message));
        } else {
          resolve(parsed.result);
        }
      } else {
        // If it's not a response to a pending request, it might be an unsolicited notification.
        console.log('Received unsolicited MCP message:', parsed);
      }
    } catch (error) {
      console.error('Failed to parse MCP message:', error);
      // For now, if parsing fails, just log and ignore.
    }
  }

  /**
   * Initializes the MCP protocol with the server
   */
  private async initialize(): Promise<void> {
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
  async executeToolCall(toolName: string, parameters: any): Promise<any> {
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
  private async sendMessage(message: MCPMessage): Promise<any> {
    return new Promise((resolve, reject) => {
      if (!this.process || !this.isConnected) {
        reject(new Error('MCP Server not connected (sendMessage called before connection)'));
        return;
      }

      // Generate unique ID for this request
      const id = message.id || Date.now().toString();
      message.id = id;

      // Store promise handlers for when response arrives
      this.pendingRequests.set(id, { resolve, reject });

      // Send message to server via stdin
      this.process.stdin?.write(JSON.stringify(message) + '\n');

      // Set timeout to prevent hanging requests
      setTimeout(() => {
        if (this.pendingRequests.has(id)) {
          this.pendingRequests.delete(id);
          reject(new Error(`Request timeout for ID: ${id}`));
        }
      }, 30000); // 30 second timeout
    });
  }

  /**
   * Stops the MCP server process
   */
  async stop(): Promise<void> {
    if (this.process) {
      console.log('Attempting to stop Todoist MCP Server process...');
      this.process.kill(); // Sends SIGTERM by default
      this.process = null;
      this.isConnected = false;
      console.log('Todoist MCP Server process stopped.');
    }
  }
}
