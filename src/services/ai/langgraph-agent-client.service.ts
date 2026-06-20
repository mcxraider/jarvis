import { LogContext, logger } from '../../utils/logger';

export type LangGraphAgentStatus = 'completed' | 'interrupted' | 'failed';

export interface LangGraphInterrupt {
  type?: string;
  question?: string;
  reason?: string;
  missing_fields?: string[];
  risk?: string;
  tool_call_id?: string;
  thread_id?: string;
  user_id?: string;
  request_source?: string;
}

export interface LangGraphAgentResponse {
  status: LangGraphAgentStatus;
  threadId: string;
  response: string;
  interrupt?: LangGraphInterrupt;
  toolResults: Record<string, unknown>[];
  error?: string;
}

export interface LangGraphAgentRequest {
  message: string;
  userId: string;
  source?: string;
  telegramUserId?: number;
  requestId?: string;
  threadId?: string;
}

interface RawAgentResponse {
  status?: LangGraphAgentStatus;
  thread_id?: string;
  response?: string;
  interrupt?: LangGraphInterrupt;
  tool_results?: Record<string, unknown>[];
  error?: string;
}

export interface LangGraphAgentClientConfig {
  baseUrl?: string;
  timeoutMs?: number;
  apiKey?: string;
}

const DEFAULT_TIMEOUT_MS = 60000;

export class LangGraphAgentClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly apiKey?: string;

  constructor(config: LangGraphAgentClientConfig = {}) {
    const baseUrl = config.baseUrl || process.env.LANGGRAPH_AGENT_URL;
    if (!baseUrl) {
      throw new Error('LANGGRAPH_AGENT_URL environment variable is required');
    }

    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.timeoutMs =
      config.timeoutMs ||
      Number(process.env.LANGGRAPH_AGENT_TIMEOUT_MS || DEFAULT_TIMEOUT_MS);
    this.apiKey = config.apiKey || process.env.LANGGRAPH_AGENT_API_KEY;
  }

  async invoke(
    request: LangGraphAgentRequest,
    logContext: LogContext = {},
  ): Promise<LangGraphAgentResponse> {
    return this.post('/invoke', request, logContext);
  }

  async resume(
    request: LangGraphAgentRequest & { threadId: string },
    logContext: LogContext = {},
  ): Promise<LangGraphAgentResponse> {
    return this.post('/resume', request, logContext);
  }

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
        hasTelegramUserId: request.telegramUserId !== undefined,
        hasThreadId: !!request.threadId,
      });

      const response = await fetch(`${this.baseUrl}${path}`, {
        method: 'POST',
        headers: this.headers(),
        body: JSON.stringify(this.toPayload(request)),
        signal: controller.signal,
      });

      const bodyText = await response.text();
      const body = this.parseBody(bodyText);

      if (!response.ok) {
        throw new Error(`LangGraph API returned ${response.status}`);
      }

      const normalized = this.normalize(body);
      logger.info('langgraph.request.completed', {
        ...logContext,
        path,
        userId: request.userId,
        status: normalized.status,
        threadId: normalized.threadId,
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

      return {
        status: 'failed',
        threadId: request.threadId || '',
        response: 'Jarvis is temporarily unavailable. Please try again in a moment.',
        toolResults: [],
        error: (error as Error).message,
      };
    } finally {
      clearTimeout(timeout);
    }
  }

  private headers(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.apiKey) {
      headers['X-Jarvis-Agent-Key'] = this.apiKey;
    }
    return headers;
  }

  private toPayload(request: LangGraphAgentRequest & { threadId?: string }): Record<string, unknown> {
    return {
      message: request.message,
      user_id: request.userId,
      source: request.source,
      telegram_user_id: request.telegramUserId,
      request_id: request.requestId,
      thread_id: request.threadId,
    };
  }

  private parseBody(bodyText: string): RawAgentResponse {
    if (!bodyText) return {};
    try {
      return JSON.parse(bodyText) as RawAgentResponse;
    } catch {
      return {};
    }
  }

  private normalize(body: RawAgentResponse): LangGraphAgentResponse {
    const status = body.status || 'failed';
    return {
      status,
      threadId: body.thread_id || '',
      response: body.response || 'Jarvis could not complete that request.',
      interrupt: body.interrupt,
      toolResults: body.tool_results || [],
      error: body.error,
    };
  }
}
