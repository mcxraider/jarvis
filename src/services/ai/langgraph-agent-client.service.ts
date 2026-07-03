// src/services/ai/langgraph-agent-client.service.ts — HTTP client for the Python
// LangGraph agent API (FastAPI backend). Supports two delivery modes:
//   1. Standard POST to /invoke or /resume — returns the full response in one shot.
//   2. Streaming POST to /invoke/stream or /resume/stream — delivers progress events
//      (stage updates) via newline-delimited JSON, ending with a "final" event payload.
// Falls back from stream to standard POST transparently if the stream fails to start.

import { LogContext, logger } from '../../utils/logger';
import { AgentResponseSchema, StreamEventSchema } from '../../types/agent.types';

export type LangGraphAgentStatus = 'completed' | 'interrupted' | 'failed';
export type LangGraphInterruptType = 'clarify' | 'confirm';

export type { LangGraphInterrupt } from '../../types/agent.types';

// The normalized response shape that all callers receive, regardless of whether
// the underlying request was standard or streamed.
export interface LangGraphAgentResponse {
  status: LangGraphAgentStatus;
  threadId: string;
  response: string;
  interrupt?: import('../../types/agent.types').LangGraphInterrupt;
  toolResults: Record<string, unknown>[];
  error?: string;
}

// Progress events emitted during streamed requests, forwarded to the Telegram
// progress reporter so users see real-time status updates (e.g. "Thinking...").
export interface LangGraphProgressEvent {
  sequence?: number;
  stage: string;
  message: string;
}

export type LangGraphProgressCallback = (event: LangGraphProgressEvent) => void | Promise<void>;

// Structured result of the Python /health/detail deep-probe: one entry per
// downstream dependency (deepseek, todoist) plus the live model name. Surfaced
// by the Telegram /status card.
export interface LangGraphDependencyCheck {
  ok: boolean;
  detail: string;
}

export interface LangGraphDependencyHealth {
  status: 'ok' | 'degraded';
  model: string;
  checks: Record<string, LangGraphDependencyCheck>;
}

export interface LangGraphAgentRequest {
  message: string;
  userId: string;
  source?: string;
  telegramUserId?: number;
  telegramUsername?: string;
  telegramFirstName?: string;
  requestId?: string;
  threadId?: string;
}

export interface LangGraphAgentClientConfig {
  baseUrl?: string;
  timeoutMs?: number;
  apiKey?: string;
}

const DEFAULT_TIMEOUT_MS = 60000;
const RETRY_DELAYS_MS = [1000, 3000];
// Health probes are user-facing (/status) and must fail fast — don't inherit the
// generous invoke/resume timeout.
const HEALTH_TIMEOUT_MS = 8000;

export class LangGraphAgentClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly apiKey?: string;

  constructor(config: LangGraphAgentClientConfig = {}) {
    const baseUrl = config.baseUrl || process.env.LANGGRAPH_AGENT_URL;
    if (!baseUrl) {
      throw new Error('LANGGRAPH_AGENT_URL environment variable is required');
    }

    // Strip trailing slashes so we can append paths without double-slash issues.
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.timeoutMs =
      config.timeoutMs ||
      Number(process.env.LANGGRAPH_AGENT_TIMEOUT_MS || DEFAULT_TIMEOUT_MS);
    this.apiKey = config.apiKey || process.env.LANGGRAPH_AGENT_API_KEY;
  }

  // Sends a new message to the agent. If a progress callback is provided, uses the
  // streaming endpoint for real-time stage updates; otherwise does a simple POST.
  async invoke(
    request: LangGraphAgentRequest,
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
  ): Promise<LangGraphAgentResponse> {
    if (onProgress) {
      return this.postStream('/invoke/stream', '/invoke', request, logContext, onProgress);
    }
    return this.post('/invoke', request, logContext);
  }

  // Resumes an interrupted conversation thread (e.g. after a HITL clarify/confirm).
  // Requires a threadId so the backend can locate the paused graph execution.
  async resume(
    request: LangGraphAgentRequest & { threadId: string },
    logContext: LogContext = {},
    onProgress?: LangGraphProgressCallback,
  ): Promise<LangGraphAgentResponse> {
    if (onProgress) {
      return this.postStream('/resume/stream', '/resume', request, logContext, onProgress);
    }
    return this.post('/resume', request, logContext);
  }

  // Probes the Python agent's deep-health endpoint for the /status card. Passes the
  // requesting Telegram user id so the backend can check that user's Todoist token.
  // Uses a short, non-retrying timeout; throws on any failure so the caller can
  // render the agent as unreachable rather than fabricating a healthy card.
  async fetchDependencyHealth(telegramUserId?: number): Promise<LangGraphDependencyHealth> {
    const url = new URL(`${this.baseUrl}/health/detail`);
    if (telegramUserId !== undefined) {
      url.searchParams.set('telegram_user_id', String(telegramUserId));
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
    try {
      const response = await fetch(url.toString(), {
        method: 'GET',
        headers: this.headers(),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`LangGraph health returned ${response.status}`);
      }
      return (await response.json()) as LangGraphDependencyHealth;
    } finally {
      clearTimeout(timeout);
    }
  }

  // Standard (non-streaming) POST to the agent API. Uses AbortController for timeout
  // management. On any failure, returns a graceful fallback response rather than throwing.
  private async post(
    path: '/invoke' | '/resume',
    request: LangGraphAgentRequest & { threadId?: string },
    logContext: LogContext,
  ): Promise<LangGraphAgentResponse> {
    const startedAt = Date.now();

    try {
      logger.info('langgraph.request.started', {
        ...logContext,
        path,
        userId: request.userId,
        hasTelegramUserId: request.telegramUserId !== undefined,
        hasThreadId: !!request.threadId,
        threadId: request.threadId,
      });

      const response = await this.fetchWithRetry(
        `${this.baseUrl}${path}`,
        {
          method: 'POST',
          headers: this.headers(),
          body: JSON.stringify(this.toPayload(request)),
        },
        logContext,
      );

      const bodyText = await response.text();

      if (!response.ok) {
        throw new Error(`LangGraph API returned ${response.status}`);
      }

      const normalized = this.parseAndNormalize(bodyText, logContext);
      logger.info('langgraph.request.completed', {
        ...logContext,
        path,
        userId: request.userId,
        status: normalized.status,
        threadId: normalized.threadId,
        requestedThreadId: request.threadId,
        durationMs: Date.now() - startedAt,
      });
      return normalized;
    } catch (error) {
      logger.error('langgraph.request.failed', {
        ...logContext,
        path,
        userId: request.userId,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      return this.fallbackResponse(request.threadId, (error as Error).message);
    }
  }

  // Streaming POST: connects to the NDJSON stream endpoint. If the stream fails
  // before any data arrives, transparently falls back to the standard POST endpoint.
  // Once the stream has started, a mid-stream failure returns a fallback error response.
  private async postStream(
    streamPath: '/invoke/stream' | '/resume/stream',
    fallbackPath: '/invoke' | '/resume',
    request: LangGraphAgentRequest & { threadId?: string },
    logContext: LogContext,
    onProgress: LangGraphProgressCallback,
  ): Promise<LangGraphAgentResponse> {
    const startedAt = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    let streamStarted = false;

    try {
      logger.info('langgraph.stream.started', {
        ...logContext,
        path: streamPath,
        userId: request.userId,
        hasTelegramUserId: request.telegramUserId !== undefined,
        hasThreadId: !!request.threadId,
        threadId: request.threadId,
      });

      const response = await this.fetchWithRetry(
        `${this.baseUrl}${streamPath}`,
        {
          method: 'POST',
          headers: this.headers(),
          body: JSON.stringify(this.toPayload(request)),
        },
        logContext,
      );

      if (!response.ok || !response.body) {
        throw new Error(`LangGraph stream returned ${response.status}`);
      }

      streamStarted = true;
      const finalResponse = await this.readStream(response.body, onProgress, logContext);
      logger.info('langgraph.stream.completed', {
        ...logContext,
        path: streamPath,
        userId: request.userId,
        status: finalResponse.status,
        threadId: finalResponse.threadId,
        requestedThreadId: request.threadId,
        durationMs: Date.now() - startedAt,
      });
      return finalResponse;
    } catch (error) {
      logger.warn('langgraph.stream.failed', {
        ...logContext,
        path: streamPath,
        userId: request.userId,
        streamStarted,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      if (!streamStarted) {
        return this.post(fallbackPath, request, logContext);
      }

      return this.fallbackResponse(request.threadId, (error as Error).message);
    } finally {
      clearTimeout(timeout);
    }
  }

  private async fetchWithRetry(
    url: string,
    init: RequestInit,
    logContext: LogContext,
  ): Promise<Response> {
    let lastError: Error | undefined;

    for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.timeoutMs);

      try {
        const response = await fetch(url, { ...init, signal: controller.signal });

        if (response.ok || response.status < 500) {
          return response;
        }

        lastError = new Error(`LangGraph API returned ${response.status}`);

        if (attempt < RETRY_DELAYS_MS.length) {
          logger.warn('langgraph.request.retrying', {
            ...logContext,
            attempt: attempt + 1,
            status: response.status,
            delayMs: RETRY_DELAYS_MS[attempt],
          });
          await new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS_MS[attempt]));
        }
      } catch (error) {
        // Don't retry aborts (timeout) or network errors
        throw error;
      } finally {
        clearTimeout(timeout);
      }
    }

    throw lastError!;
  }

  // Reads the streaming response body chunk-by-chunk, splitting on newlines to get
  // individual JSON events. Progress events trigger the callback; the "final" event
  // contains the complete agent response and terminates the stream.
  private async readStream(
    body: ReadableStream<Uint8Array>,
    onProgress: LangGraphProgressCallback,
    logContext: LogContext,
  ): Promise<LangGraphAgentResponse> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalResponse: LangGraphAgentResponse | undefined;

    while (true) {
      const { value, done } = await reader.read();
      if (value) {
        buffer += decoder.decode(value, { stream: true });
        finalResponse = await this.consumeStreamBuffer(buffer, onProgress, finalResponse, logContext);
        buffer = this.remainingPartialLine(buffer);
      }
      if (done) break;
    }

    buffer += decoder.decode();
    if (buffer.trim()) {
      finalResponse = await this.consumeStreamLine(buffer.trim(), onProgress, finalResponse, logContext);
    }

    if (!finalResponse) {
      throw new Error('LangGraph stream ended without a final response');
    }

    return finalResponse;
  }

  // Processes all complete lines in the buffer (everything except the last partial line).
  private async consumeStreamBuffer(
    buffer: string,
    onProgress: LangGraphProgressCallback,
    finalResponse: LangGraphAgentResponse | undefined,
    logContext: LogContext,
  ): Promise<LangGraphAgentResponse | undefined> {
    const lines = buffer.split(/\r?\n/);
    const completeLines = lines.slice(0, -1);
    let latestFinal = finalResponse;

    for (const line of completeLines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      latestFinal = await this.consumeStreamLine(trimmed, onProgress, latestFinal, logContext);
    }

    return latestFinal;
  }

  // Returns the trailing incomplete line (no terminating newline yet) for the next chunk.
  private remainingPartialLine(buffer: string): string {
    const lines = buffer.split(/\r?\n/);
    return lines[lines.length - 1] || '';
  }

  // Parses a single NDJSON line into a typed stream event. Dispatches progress events
  // to the callback and captures the final response payload when the stream terminates.
  private async consumeStreamLine(
    line: string,
    onProgress: LangGraphProgressCallback,
    finalResponse: LangGraphAgentResponse | undefined,
    logContext: LogContext,
  ): Promise<LangGraphAgentResponse | undefined> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      return finalResponse;
    }

    const result = StreamEventSchema.safeParse(parsed);
    if (!result.success) {
      logger.warn('langgraph.stream.unknown_event', { ...logContext });
      return finalResponse;
    }

    const event = result.data;
    if (event.type === 'progress') {
      await onProgress({ sequence: event.sequence, stage: event.stage, message: event.message });
      return finalResponse;
    }

    if (event.type === 'final') {
      return this.normalize(event.response);
    }

    return finalResponse;
  }

  // Parses the raw JSON body from a non-streaming response and validates it against
  // the Zod schema. Returns a normalized response or a safe fallback on parse failures.
  private parseAndNormalize(bodyText: string, logContext: LogContext): LangGraphAgentResponse {
    if (!bodyText) {
      return this.fallbackResponse();
    }

    let raw: unknown;
    try {
      raw = JSON.parse(bodyText);
    } catch {
      logger.warn('langgraph.response.parse_failed', { ...logContext });
      return this.fallbackResponse();
    }

    const result = AgentResponseSchema.safeParse(raw);
    if (!result.success) {
      logger.warn('langgraph.response.validation_error', {
        ...logContext,
        issues: result.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; '),
      });
      return this.fallbackResponse();
    }

    return this.normalize(result.data);
  }

  private headers(): Record<string, string> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (this.apiKey) {
      headers['X-Jarvis-Agent-Key'] = this.apiKey;
    }
    return headers;
  }

  // Converts the TypeScript-shaped request to the snake_case payload the Python API expects.
  private toPayload(request: LangGraphAgentRequest & { threadId?: string }): Record<string, unknown> {
    return {
      message: request.message,
      user_id: request.userId,
      source: request.source,
      telegram_user_id: request.telegramUserId,
      telegram_username: request.telegramUsername,
      telegram_first_name: request.telegramFirstName,
      request_id: request.requestId,
      thread_id: request.threadId,
    };
  }

  // Maps the Python API's snake_case response into the camelCase interface our TS code uses.
  private normalize(body: {
    status?: string;
    thread_id?: string;
    response?: string;
    interrupt?: import('../../types/agent.types').LangGraphInterrupt | null;
    tool_results?: Record<string, unknown>[] | null;
    error?: string | null;
  }): LangGraphAgentResponse {
    const status = (body.status as LangGraphAgentStatus) || 'failed';
    return {
      status,
      threadId: body.thread_id || '',
      response: body.response || 'Jarvis could not complete that request.',
      interrupt: body.interrupt ?? undefined,
      toolResults: body.tool_results || [],
      error: body.error ?? undefined,
    };
  }

  // User-safe error response when the agent API is unreachable or returns garbage.
  private fallbackResponse(threadId?: string, error?: string): LangGraphAgentResponse {
    return {
      status: 'failed',
      threadId: threadId || '',
      response: 'Jarvis is temporarily unavailable. Please try again in a moment.',
      toolResults: [],
      ...(error && { error }),
    };
  }
}
