import { TextProcessorService } from '../../../../../src/services/telegram/processors/text-processor.service';
import { CallbackHandler } from '../../../../../src/services/telegram/handlers/callback-handler';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { LangGraphAgentResponse } from '../../../../../src/services/ai/langgraph-agent-client.service';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgentResponseSequence {
  invoke?: LangGraphAgentResponse[];
  resume?: LangGraphAgentResponse[];
}

export interface MockAgentClient {
  invoke: jest.Mock;
  resume: jest.Mock;
}

export interface CtxOpts {
  userId?: number;
  chatId?: number;
  messageId?: number;
}

export interface FakeContext {
  update?: { __requestId?: string };
  from: { id: number };
  chat: { id: number };
  message?: Record<string, unknown>;
  callbackQuery?: { data: string; message?: { text: string; chat: { id: number } } };
  reply: jest.Mock;
  answerCbQuery: jest.Mock;
  editMessageText: jest.Mock;
  editMessageReplyMarkup?: jest.Mock;
  telegram: { editMessageText: jest.Mock; deleteMessage: jest.Mock };
}

export interface FlowHarness {
  agentClient: MockAgentClient;
  textProcessor: TextProcessorService;
  callbackHandler: CallbackHandler;
  store: MemoryPendingClarificationStore;
  sendText(text: string, opts?: CtxOpts): Promise<{ ctx: FakeContext; result: any }>;
  pressButton(data: string, opts?: CtxOpts): Promise<{ ctx: FakeContext }>;
}

// ---------------------------------------------------------------------------
// Factories
// ---------------------------------------------------------------------------

export function createMockAgentClient(responses: AgentResponseSequence = {}): MockAgentClient {
  const invokeResponses = responses.invoke || [];
  const resumeResponses = responses.resume || [];

  const invoke = jest.fn();
  for (const r of invokeResponses) {
    invoke.mockResolvedValueOnce(r);
  }

  const resume = jest.fn();
  for (const r of resumeResponses) {
    resume.mockResolvedValueOnce(r);
  }

  return { invoke, resume };
}

export function createTextCtx(text: string, opts: CtxOpts = {}): FakeContext {
  const { userId = 42, chatId = 100, messageId = 1 } = opts;
  return {
    from: { id: userId },
    chat: { id: chatId },
    message: { text, message_id: messageId, chat: { id: chatId } },
    reply: jest.fn().mockResolvedValue({ message_id: 77 }),
    answerCbQuery: jest.fn().mockResolvedValue(undefined),
    editMessageText: jest.fn().mockResolvedValue(undefined),
    telegram: {
      editMessageText: jest.fn().mockResolvedValue(true),
      deleteMessage: jest.fn().mockResolvedValue(true),
    },
  };
}

export function createCallbackCtx(data: string, opts: CtxOpts & { originalText?: string } = {}): FakeContext {
  const { userId = 42, chatId = 100, originalText = '⚠️ Confirm: action' } = opts;
  return {
    from: { id: userId },
    chat: { id: chatId },
    callbackQuery: {
      data,
      message: { text: originalText, chat: { id: chatId } },
    },
    reply: jest.fn().mockResolvedValue({ message_id: 88 }),
    answerCbQuery: jest.fn().mockResolvedValue(undefined),
    editMessageText: jest.fn().mockResolvedValue(undefined),
    editMessageReplyMarkup: jest.fn().mockResolvedValue(undefined),
    telegram: {
      editMessageText: jest.fn().mockResolvedValue(true),
      deleteMessage: jest.fn().mockResolvedValue(true),
    },
  };
}

export function createFlowHarness(agentClient: MockAgentClient): FlowHarness {
  const store = new MemoryPendingClarificationStore();
  const gateStore = new MemoryConversationGateStore();
  const textProcessor = new TextProcessorService(agentClient as any, store, gateStore);
  const callbackHandler = new CallbackHandler(agentClient as any, store, gateStore);
  let callbackSequence = 0;

  return {
    agentClient,
    textProcessor,
    callbackHandler,
    store,

    async sendText(text: string, opts: CtxOpts = {}) {
      const { userId = 42, chatId = 100, messageId = 1 } = opts;
      const result = await textProcessor.processTextMessage(text, userId, {
        requestId: 'tg_flow_test',
        chatId,
        messageId,
      });
      const ctx = createTextCtx(text, opts);
      return { ctx, result };
    },

    async pressButton(data: string, opts: CtxOpts = {}) {
      const ctx = createCallbackCtx(data, opts);
      ctx.update = { __requestId: `tg_flow_callback_${++callbackSequence}` };
      await callbackHandler.handleCallbackQuery(ctx as any);
      return { ctx };
    },
  };
}

// ---------------------------------------------------------------------------
// Response builders
// ---------------------------------------------------------------------------

export function interruptResponse(opts: {
  threadId: string;
  message: string;
  type?: 'confirm' | 'clarify';
  summary?: string;
}): LangGraphAgentResponse {
  return {
    status: 'interrupted',
    delivery: 'terminal',
    threadId: opts.threadId,
    response: opts.message,
    interrupt: {
      type: opts.type || 'confirm',
      question: opts.message,
      summary: opts.summary || opts.message,
    },
    toolResults: [],
  };
}

export function completedResponse(opts: {
  threadId: string;
  message: string;
}): LangGraphAgentResponse {
  return {
    status: 'completed',
    delivery: 'terminal',
    threadId: opts.threadId,
    response: opts.message,
    toolResults: [],
  };
}

export function failedResponse(opts: {
  threadId?: string;
  message?: string;
  error?: string;
}): LangGraphAgentResponse {
  return {
    status: 'failed',
    delivery: 'terminal',
    threadId: opts.threadId || '',
    response: opts.message || 'Jarvis is temporarily unavailable. Please try again in a moment.',
    toolResults: [],
    error: opts.error || 'connection refused',
  };
}
