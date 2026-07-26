// src/services/ai/langgraph-agent-client.service.ts — HTTP client for the Python
// LangGraph agent API (FastAPI backend). Supports two delivery modes:
//   1. Standard POST to /invoke or /resume — returns the full response in one shot.
//   2. Streaming POST to /invoke/stream or /resume/stream — delivers progress events
//      (stage updates) via newline-delimited JSON, ending with a "final" event payload.
// Falls back from stream to standard POST transparently if the stream fails to start.

import { LogContext, logger } from '../../utils/logger';
import {
  AgentHealthDetail,
  AgentHealthDetailSchema,
  AgentResponseSchema,
  ProgressFact,
  TelegramIdentityPayload,
  StreamEventSchema,
} from '../../types/agent.types';
import { resolveLangGraphClientTimeouts } from '../../config/turn-timeout.config';

export type LangGraphAgentStatus = 'completed' | 'interrupted' | 'failed';
export type LangGraphDelivery = 'terminal' | 'ambiguous';
export type LangGraphInterruptType = 'clarify' | 'confirm';
export type LangGraphCancelOutcome =
  | 'cancelled'
  | 'mutation_in_flight'
  | 'already_finished'
  | 'not_found';

export type { LangGraphInterrupt } from '../../types/agent.types';

// The normalized response shape that all callers receive, regardless of whether
// the underlying request was standard or streamed.
export interface LangGraphAgentResponse {
  status: LangGraphAgentStatus;
  /**
   * `terminal` means the backend returned a valid final envelope. `ambiguous`
   * means the transport failed after the request may have been accepted, so
   * callers must retain ownership and must not automatically replay it.
   */
  delivery: LangGraphDelivery;
  threadId: string;
  response: string;
  reasoningContent?: string;
  interrupt?: import('../../types/agent.types').LangGraphInterrupt;
  toolResults: Record<string, unknown>[];
  error?: string;
  errorDetails?: Record<string, unknown>;
}

// Progress events emitted during streamed requests, forwarded to the Telegram
// progress reporter so users see real-time status updates (e.g. "Thinking...").
export interface LangGraphProgressEvent {
  sequence?: number;
  stage: string;
  message: string;
  fact?: ProgressFact;
  narration?: string;
  metadata?: Record<string, unknown>;
}

export type LangGraphProgressCallback = (
  event: LangGraphProgressEvent,
  signal?: AbortSignal,
) => void | Promise<void>;

// Structured result of the Python /health/detail deep-probe: one entry per
// downstream dependency (deepseek, todoist) plus the live model name. Surfaced
// by the Telegram /status card.
export type LangGraphDependencyHealth = AgentHealthDetail;

export interface TelegramIdentity {
  telegramId: TelegramIdentityPayload['telegram_id'];
  username?: TelegramIdentityPayload['username'];
}

export interface LangGraphAgentRequest {
  message: string;
  userId: string;
  source?: string;
  telegramIdentity?: TelegramIdentity;
  requestId?: string;
  threadId?: string;
  replyContext?: { role: 'assistant' | 'user'; message: string };
}

export interface LangGraphAgentClientConfig {
  baseUrl?: string;
  timeoutMs?: number;
  streamIdleTimeoutMs?: number;
  apiKey?: string;
}

// Fraction of the overall budget past which a completed turn is worth flagging.
// Observed latency is p50 8s / p95 28s / p99 57s, so crossing ~109s of a 165s budget
// is rare and meaningful: it surfaces creeping latency and future ladder inversions
// before they reach users as a hard abort.
const NEAR_TIMEOUT_RATIO = 0.66;
const PROGRESS_CALLBACK_TIMEOUT_MS = 5000;
const RETRY_DELAYS_MS = [1000, 3000];
// Health probes are user-facing (/status) and must fail fast — don't inherit the
// generous invoke/resume timeout.
const HEALTH_TIMEOUT_MS = 8000;
const CANCEL_TIMEOUT_MS = 5000;
const CANCEL_OUTCOMES = new Set<LangGraphCancelOutcome>([
  'cancelled',
  'mutation_in_flight',
  'already_finished',
  'not_found',
]);

// A completed 4xx response (other than 409) is a pre-admission rejection: the
// backend has told us that it did not accept this request for execution. A 409
// can represent an in-flight idempotency conflict, so its delivery remains
// ambiguous and callers must retain ownership rather than replaying it.
function isTerminalHttpRejection(status: number): boolean {
  return status >= 400 && status < 500 && status !== 409;
}

type StreamFailureKind =
  | 'overall_timeout'
  | 'idle_timeout'
  | 'premature_eof'
  | 'stream_error'
  | 'http_error'
  | 'connection';

export class LangGraphAgentClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly streamIdleTimeoutMs: number;
  private readonly apiKey?: string;

  constructor(config: LangGraphAgentClientConfig = {}) {
    const baseUrl = config.baseUrl || process.env.LANGGRAPH_AGENT_URL;
    if (!baseUrl) {
      throw new Error('LANGGRAPH_AGENT_URL environment variable is required');
    }

    // Strip trailing slashes so we can append paths without double-slash issues.
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    const timeouts = resolveLangGraphClientTimeouts(config);
    this.timeoutMs = timeouts.overallMs;
    this.streamIdleTimeoutMs = timeouts.streamIdleMs;
    this.apiKey = config.apiKey || process.env.LANGGRAPH_AGENT_API_KEY;
  }

  getRuntimeTimeouts(): { overallMs: number; streamIdleMs: number } {
    return {
      overallMs: this.timeoutMs,
      streamIdleMs: this.streamIdleTimeoutMs,
    };
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
      return AgentHealthDetailSchema.parse(await response.json());
    } finally {
      clearTimeout(timeout);
    }
  }

  /** Ask the Python service to cooperatively cancel one accepted run. */
  async cancelRun(userId: string, requestId: string): Promise<LangGraphCancelOutcome> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), CANCEL_TIMEOUT_MS);
    try {
      const response = await fetch(`${this.baseUrl}/runs/cancel`, {
        method: 'POST',
        headers: this.headers(),
        body: JSON.stringify({ user_id: userId, request_id: requestId }),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`LangGraph cancel returned ${response.status}`);
      }
      const body = (await response.json()) as { outcome?: unknown };
      if (
        typeof body.outcome !== 'string' ||
        !CANCEL_OUTCOMES.has(body.outcome as LangGraphCancelOutcome)
      ) {
        throw new Error('LangGraph cancel returned an invalid outcome');
      }
      return body.outcome as LangGraphCancelOutcome;
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
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      logger.info('langgraph.request.started', {
        ...logContext,
        path,
        userId: request.userId,
        hasTelegramIdentity: request.telegramIdentity !== undefined,
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
        controller.signal,
        !!request.requestId,
      );

      const bodyText = await this.awaitWithAbort(response.text(), controller.signal);

      if (!response.ok) {
        const error = `LangGraph API returned ${response.status}`;
        if (isTerminalHttpRejection(response.status)) {
          return this.fallbackResponse(request.threadId, error, 'terminal');
        }
        throw new Error(error);
      }

      const normalized = this.parseAndNormalize(bodyText, logContext, request.threadId);
      logger.info('langgraph.request.completed', {
        ...logContext,
        path,
        userId: request.userId,
        status: normalized.status,
        agentError: normalized.error,
        ...this.backendErrorLogFields(normalized.errorDetails),
        threadId: normalized.threadId,
        requestedThreadId: request.threadId,
        durationMs: Date.now() - startedAt,
      });
      return normalized;
    } catch (error) {
      if (!controller.signal.aborted) controller.abort();
      logger.error('langgraph.request.failed', {
        ...logContext,
        path,
        userId: request.userId,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      return this.fallbackResponse(request.threadId, (error as Error).message, 'ambiguous');
    } finally {
      clearTimeout(timeout);
    }
  }

  // Streaming POST: connects to the NDJSON stream endpoint. A pre-header failure can
  // fall back to the standard endpoint only when the request carries an idempotency
  // key. Once headers arrive, or after either deadline fires, never re-POST: the
  // backend may already be executing a mutation.
  private async postStream(
    streamPath: '/invoke/stream' | '/resume/stream',
    fallbackPath: '/invoke' | '/resume',
    request: LangGraphAgentRequest & { threadId?: string },
    logContext: LogContext,
    onProgress: LangGraphProgressCallback,
  ): Promise<LangGraphAgentResponse> {
    const startedAt = Date.now();
    // This controller is passed directly to fetch and remains attached while the
    // response body is read, so either deadline tears down the live socket.
    const controller = new AbortController();
    let deadlineKind: 'overall' | 'idle' | null = null;
    let responseReceived = false;
    let streamStarted = false;

    const abortWith = (kind: 'overall' | 'idle') => {
      if (deadlineKind || controller.signal.aborted) return;
      deadlineKind = kind;
      controller.abort();
    };

    const overallTimer = setTimeout(() => abortWith('overall'), this.timeoutMs);
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const armIdleTimer = () => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => abortWith('idle'), this.streamIdleTimeoutMs);
    };

    try {
      logger.info('langgraph.stream.started', {
        ...logContext,
        path: streamPath,
        userId: request.userId,
        hasTelegramIdentity: request.telegramIdentity !== undefined,
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
        controller.signal,
        false,
        () => {
          responseReceived = true;
        },
      );

      if (!response.ok) {
        const error = `LangGraph stream returned ${response.status}`;
        if (isTerminalHttpRejection(response.status)) {
          // Do not classify a rejection as terminal until its response body is
          // readable. A socket failure after headers still leaves delivery
          // uncertain, even when the received status was 4xx.
          await this.awaitWithAbort(response.text(), controller.signal);
          return this.fallbackResponse(request.threadId, error, 'terminal');
        }
        throw new Error(error);
      }

      if (!response.body) {
        throw new Error(`LangGraph stream returned ${response.status}`);
      }

      streamStarted = true;
      armIdleTimer();
      const finalResponse = await this.readStream(
        response.body,
        onProgress,
        logContext,
        armIdleTimer,
        controller.signal,
      );
      const durationMs = Date.now() - startedAt;
      logger.info('langgraph.stream.completed', {
        ...logContext,
        path: streamPath,
        userId: request.userId,
        status: finalResponse.status,
        agentError: finalResponse.error,
        ...this.backendErrorLogFields(finalResponse.errorDetails),
        threadId: finalResponse.threadId,
        requestedThreadId: request.threadId,
        durationMs,
      });
      this.reportNearTimeout(durationMs, streamPath, logContext);
      return finalResponse;
    } catch (error) {
      const failureKind = this.classifyStreamFailure(
        deadlineKind,
        responseReceived,
        streamStarted,
        error as Error,
      );
      // Tear down any still-readable response body on parse/callback/connection
      // failures as well as on explicit deadlines.
      if (!controller.signal.aborted) controller.abort();
      logger.warn('langgraph.stream.failed', {
        ...logContext,
        path: streamPath,
        userId: request.userId,
        responseReceived,
        streamStarted,
        failureKind,
        error: (error as Error).message,
        durationMs: Date.now() - startedAt,
      });

      // Retrying or switching endpoints is safe only when the backend can collapse
      // an ambiguous duplicate using the same request id.
      if (!responseReceived && !streamStarted && !deadlineKind && !!request.requestId) {
        return this.post(fallbackPath, request, logContext);
      }

      return this.fallbackResponse(
        request.threadId,
        this.streamFailureMessage(failureKind, error as Error),
        'ambiguous',
      );
    } finally {
      clearTimeout(overallTimer);
      if (idleTimer) clearTimeout(idleTimer);
    }
  }

  // A turn that succeeded but consumed most of its budget is the early warning for
  // the failure mode this ladder exists to prevent: the next slightly slower turn
  // aborts instead. The client owns the budget, so it is the layer that can see this.
  private reportNearTimeout(durationMs: number, path: string, logContext: LogContext): void {
    if (durationMs <= NEAR_TIMEOUT_RATIO * this.timeoutMs) return;
    logger.warn('langgraph.turn.near_timeout', {
      ...logContext,
      path,
      durationMs,
      timeoutMs: this.timeoutMs,
      thresholdRatio: NEAR_TIMEOUT_RATIO,
    });
  }

  private classifyStreamFailure(
    deadlineKind: 'overall' | 'idle' | null,
    responseReceived: boolean,
    streamStarted: boolean,
    error: Error,
  ): StreamFailureKind {
    if (deadlineKind === 'overall') return 'overall_timeout';
    if (deadlineKind === 'idle') return 'idle_timeout';
    if (error.message === 'LangGraph stream ended without a final response') {
      return 'premature_eof';
    }
    if (streamStarted) return 'stream_error';
    return responseReceived ? 'http_error' : 'connection';
  }

  private streamFailureMessage(kind: StreamFailureKind, error: Error): string {
    if (kind === 'overall_timeout') {
      return 'LangGraph stream timed out (overall deadline exceeded)';
    }
    if (kind === 'idle_timeout') {
      return 'LangGraph stream timed out (idle: no data received)';
    }
    return error.message;
  }

  private async fetchWithRetry(
    url: string,
    init: RequestInit,
    logContext: LogContext,
    signal: AbortSignal,
    allowRetries = true,
    onResponse?: () => void,
  ): Promise<Response> {
    let lastError: Error | undefined;
    const retryDelays = allowRetries ? RETRY_DELAYS_MS : [];

    for (let attempt = 0; attempt <= retryDelays.length; attempt++) {
      try {
        const response = await fetch(url, {
          ...init,
          signal,
        });
        onResponse?.();

        if (response.ok || response.status < 500) {
          return response;
        }

        lastError = new Error(`LangGraph API returned ${response.status}`);

        if (attempt < retryDelays.length) {
          if (response.body) {
            await this.awaitWithAbort(response.body.cancel(), signal);
          }
          logger.warn('langgraph.request.retrying', {
            ...logContext,
            attempt: attempt + 1,
            status: response.status,
            delayMs: retryDelays[attempt],
          });
          await this.waitForRetry(retryDelays[attempt], signal);
        }
      } catch (error) {
        // Don't retry aborts (timeout) or network errors
        throw error;
      }
    }

    throw lastError!;
  }

  private async waitForRetry(delayMs: number, signal: AbortSignal): Promise<void> {
    if (signal.aborted) {
      throw signal.reason || new DOMException('The operation was aborted', 'AbortError');
    }

    await new Promise<void>((resolve, reject) => {
      const onAbort = () => {
        signal.removeEventListener('abort', onAbort);
        clearTimeout(timeout);
        reject(signal.reason || new DOMException('The operation was aborted', 'AbortError'));
      };
      const timeout = setTimeout(() => {
        signal.removeEventListener('abort', onAbort);
        resolve();
      }, delayMs);
      signal.addEventListener('abort', onAbort, { once: true });
      if (signal.aborted) onAbort();
    });
  }

  private async awaitWithAbort<T>(operation: Promise<T>, signal: AbortSignal): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      let settled = false;
      const cleanup = () => signal.removeEventListener('abort', onAbort);
      const onAbort = () => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(signal.reason || new DOMException('The operation was aborted', 'AbortError'));
      };
      signal.addEventListener('abort', onAbort, { once: true });
      operation.then(
        (value) => {
          if (settled) return;
          settled = true;
          cleanup();
          resolve(value);
        },
        (error) => {
          if (settled) return;
          settled = true;
          cleanup();
          reject(error);
        },
      );
      if (signal.aborted) onAbort();
    });
  }

  // Reads the streaming response body chunk-by-chunk, splitting on newlines to get
  // individual JSON events. Progress events trigger the callback; the "final" event
  // contains the complete agent response and terminates the stream.
  private async readStream(
    body: ReadableStream<Uint8Array>,
    onProgress: LangGraphProgressCallback,
    logContext: LogContext,
    onChunk: () => void,
    signal: AbortSignal,
  ): Promise<LangGraphAgentResponse> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalResponse: LangGraphAgentResponse | undefined;
    let progressCallbackEnabled = true;
    const reportProgress: LangGraphProgressCallback = async (event) => {
      if (!progressCallbackEnabled) return;
      const callbackStillEnabled = await this.deliverProgress(
        event,
        onProgress,
        logContext,
        signal,
      );
      this.throwIfAborted(signal);
      progressCallbackEnabled = callbackStillEnabled;
    };

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (value) {
          this.throwIfAborted(signal);
          onChunk();
          buffer += decoder.decode(value, { stream: true });
          const consumed = await this.consumeStreamBuffer(
            buffer,
            reportProgress,
            finalResponse,
            logContext,
          );
          finalResponse = consumed.response;
          buffer = consumed.remainder;
          if (finalResponse) return finalResponse;
        }
        if (done) break;
      }

      buffer += decoder.decode();
      if (buffer.trim()) {
        this.throwIfAborted(signal);
        finalResponse = await this.consumeStreamLine(
          buffer.trim(),
          reportProgress,
          finalResponse,
          logContext,
        );
      }

      if (!finalResponse) {
        throw new Error('LangGraph stream ended without a final response');
      }

      return finalResponse;
    } finally {
      if (finalResponse) {
        try {
          void reader.cancel().catch(() => {});
        } catch {
          // Best effort: the terminal event is authoritative even if cancellation fails.
        }
      }
      try {
        reader.releaseLock();
      } catch {
        // Best effort: an aborted or errored stream may already have released it.
      }
    }
  }

  private async deliverProgress(
    event: LangGraphProgressEvent,
    onProgress: LangGraphProgressCallback,
    logContext: LogContext,
    signal: AbortSignal,
  ): Promise<boolean> {
    if (signal.aborted) return false;

    let timeout: ReturnType<typeof setTimeout> | undefined;
    let onAbort: (() => void) | undefined;
    const callbackController = new AbortController();
    const callback = Promise.resolve()
      .then(() => onProgress(event, callbackController.signal))
      .then(() => 'completed' as const);
    const timedOut = new Promise<'timed_out'>((resolve) => {
      timeout = setTimeout(() => {
        callbackController.abort();
        resolve('timed_out');
      }, PROGRESS_CALLBACK_TIMEOUT_MS);
    });
    const aborted = new Promise<'aborted'>((resolve) => {
      onAbort = () => {
        callbackController.abort();
        resolve('aborted');
      };
      signal.addEventListener('abort', onAbort, { once: true });
      if (signal.aborted) onAbort();
    });

    try {
      const outcome = await Promise.race([callback, timedOut, aborted]);
      if (outcome === 'completed') return true;
      if (outcome === 'timed_out') {
        logger.warn('langgraph.stream.progress_callback_timed_out', {
          ...logContext,
          stage: event.stage,
          timeoutMs: PROGRESS_CALLBACK_TIMEOUT_MS,
        });
      }
      return false;
    } catch (error) {
      logger.warn('langgraph.stream.progress_callback_failed', {
        ...logContext,
        stage: event.stage,
        error: error instanceof Error ? error.message : String(error),
      });
      return false;
    } finally {
      if (timeout) clearTimeout(timeout);
      if (onAbort) signal.removeEventListener('abort', onAbort);
    }
  }

  private throwIfAborted(signal: AbortSignal): void {
    if (signal.aborted) {
      throw signal.reason || new DOMException('The operation was aborted', 'AbortError');
    }
  }

  // Processes all complete lines in the buffer, returns final response and the trailing partial line.
  private async consumeStreamBuffer(
    buffer: string,
    onProgress: LangGraphProgressCallback,
    finalResponse: LangGraphAgentResponse | undefined,
    logContext: LogContext,
  ): Promise<{ response: LangGraphAgentResponse | undefined; remainder: string }> {
    const lines = buffer.split(/\r?\n/);
    const remainder = lines[lines.length - 1] || '';
    const completeLines = lines.slice(0, -1);
    let latestFinal = finalResponse;

    for (const line of completeLines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      latestFinal = await this.consumeStreamLine(trimmed, onProgress, latestFinal, logContext);
      if (latestFinal) break;
    }

    return { response: latestFinal, remainder };
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
      logger.warn('langgraph.stream.malformed_line', { ...logContext });
      return finalResponse;
    }

    const result = StreamEventSchema.safeParse(parsed);
    if (!result.success) {
      logger.warn('langgraph.stream.unknown_event', { ...logContext });
      return finalResponse;
    }

    const event = result.data;
    if (event.type === 'narration') {
      await onProgress({
        sequence: event.sequence,
        stage: 'narration',
        message: event.text,
        narration: event.text,
      });
      return finalResponse;
    }

    if (event.type === 'progress') {
      await onProgress({
        sequence: event.sequence,
        stage: event.stage || 'progress',
        message: event.message || 'Jarvis is working',
        ...(event.fact && { fact: event.fact }),
        ...(event.metadata && { metadata: event.metadata }),
      });
      return finalResponse;
    }

    if (event.type === 'final') {
      return this.normalize(event.response);
    }

    return finalResponse;
  }

  // Parses the raw JSON body from a non-streaming response and validates it against
  // the Zod schema. An invalid body is delivery-ambiguous: the backend accepted the
  // request but failed to return a usable terminal envelope.
  private parseAndNormalize(
    bodyText: string,
    logContext: LogContext,
    threadId?: string,
  ): LangGraphAgentResponse {
    if (!bodyText) {
      return this.fallbackResponse(
        threadId,
        'LangGraph API returned an empty response',
        'ambiguous',
      );
    }

    let raw: unknown;
    try {
      raw = JSON.parse(bodyText);
    } catch {
      logger.warn('langgraph.response.parse_failed', { ...logContext });
      return this.fallbackResponse(threadId, 'LangGraph API returned invalid JSON', 'ambiguous');
    }

    const result = AgentResponseSchema.safeParse(raw);
    if (!result.success) {
      logger.warn('langgraph.response.validation_error', {
        ...logContext,
        issues: result.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; '),
      });
      return this.fallbackResponse(
        threadId,
        'LangGraph API returned an invalid response',
        'ambiguous',
      );
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
  private toPayload(
    request: LangGraphAgentRequest & { threadId?: string },
  ): Record<string, unknown> {
    return {
      message: request.message,
      user_id: request.userId,
      source: request.source,
      telegram_identity: request.telegramIdentity
        ? {
            telegram_id: request.telegramIdentity.telegramId,
            username: request.telegramIdentity.username,
          }
        : undefined,
      request_id: request.requestId,
      thread_id: request.threadId,
      reply_context: request.replyContext,
    };
  }

  // Maps the Python API's snake_case response into the camelCase interface our TS code uses.
  private normalize(body: {
    status?: string;
    thread_id?: string;
    response?: string;
    reasoning_content?: string | null;
    interrupt?: import('../../types/agent.types').LangGraphInterrupt | null;
    tool_results?: Record<string, unknown>[] | null;
    error?: string | null;
    error_details?: Record<string, unknown> | null;
  }): LangGraphAgentResponse {
    const status = (body.status as LangGraphAgentStatus) || 'failed';
    return {
      status,
      delivery: 'terminal',
      threadId: body.thread_id || '',
      response: body.response || 'Jarvis could not complete that request.',
      reasoningContent: body.reasoning_content ?? undefined,
      interrupt: body.interrupt ?? undefined,
      toolResults: body.tool_results || [],
      error: body.error ?? undefined,
      errorDetails: body.error_details ?? undefined,
    };
  }

  private backendErrorLogFields(details?: Record<string, unknown>): Record<string, unknown> {
    if (!details) return {};
    return {
      backendErrorSource: details.source,
      backendErrorType: details.type,
      backendErrorRetryable: details.retryable,
      backendErrorAttempts: details.attempts,
      backendErrorTimeoutKind: details.timeout_kind,
      backendErrorRequestTimeoutSeconds: details.request_timeout_seconds,
      backendErrorTotalElapsedMs: details.total_elapsed_ms,
      backendErrorProviderRequestId: details.provider_request_id,
    };
  }

  // User-safe error response when the agent API is unreachable or returns garbage.
  private fallbackResponse(
    threadId?: string,
    error?: string,
    delivery: LangGraphDelivery = 'terminal',
  ): LangGraphAgentResponse {
    const timedOut = /abort|timed?\s*out/i.test(error || '');
    return {
      status: 'failed',
      delivery,
      threadId: threadId || '',
      response:
        delivery === 'ambiguous'
          ? timedOut
            ? 'I wasn’t able to finish this in time or confirm the result. This request may still be running, so I’m keeping it active to prevent a duplicate. Use /cancel if you want to stop it.'
            : 'I lost the connection before I could confirm the result. This request may still have completed, so I’m keeping it active to prevent a duplicate. Use /cancel if you want to stop it.'
          : timedOut
            ? 'I wasn’t able to finish this in time. Please try again in a moment.'
            : 'Jarvis is temporarily unavailable. Please try again in a moment.',
      toolResults: [],
      ...(error && { error }),
    };
  }
}
