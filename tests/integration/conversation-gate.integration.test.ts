import { TextProcessorService } from '../../src/services/telegram/processors/text-processor.service';
import { CallbackHandler } from '../../src/services/telegram/handlers/callback-handler';
import { MemoryPendingClarificationStore } from '../../src/services/telegram/pending-clarification.store';
import { MemoryConversationGateStore } from '../../src/services/telegram/conversation-gate.store';
import { buildConversationKey } from '../../src/services/telegram/conversation-key';

function createHarness(agentClient: any) {
  const pendingStore = new MemoryPendingClarificationStore();
  const gateStore = new MemoryConversationGateStore();
  const textProcessor = new TextProcessorService(agentClient, pendingStore, gateStore);
  const callbackHandler = new CallbackHandler(agentClient, pendingStore, gateStore);
  return { textProcessor, callbackHandler, pendingStore, gateStore };
}

function makeCtx(data: string, userId = 42, chatId = 100) {
  return {
    callbackQuery: { data, message: { text: 'Confirm?', chat: { id: chatId } } },
    from: { id: userId },
    chat: { id: chatId },
    answerCbQuery: jest.fn().mockResolvedValue(undefined),
    editMessageText: jest.fn().mockResolvedValue(undefined),
    editMessageReplyMarkup: jest.fn().mockResolvedValue(undefined),
    reply: jest.fn().mockResolvedValue(undefined),
  } as any;
}

const LOG = { chatId: 100, messageId: 1, requestId: 'test' };

describe('Conversation Gating — Integration', () => {
  describe('basic flow: message → running → complete → idle', () => {
    it('first message acquires gate and invokes agent', async () => {
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({ status: 'completed', threadId: 't1', response: 'Done.', toolResults: [] }),
        resume: jest.fn(),
      };
      const { textProcessor } = createHarness(agentClient);

      const result = await textProcessor.processTextMessage('hello', 42, LOG);
      expect(result.response).toBe('Done.');
      expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    });

    it('second concurrent message is blocked with friendly response', async () => {
      const agentClient = {
        invoke: jest.fn().mockImplementation(() => new Promise(() => {})),
        resume: jest.fn(),
      };
      const { textProcessor } = createHarness(agentClient);

      textProcessor.processTextMessage('first', 42, { ...LOG, messageId: 1 });
      await new Promise((r) => setTimeout(r, 10));

      const result = await textProcessor.processTextMessage('second', 42, { ...LOG, messageId: 2 });
      expect(result.blocked).toBe(true);
      expect(result.response).toMatch(/still working/i);
    });

    it('after first completes, gate is idle and new message proceeds', async () => {
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({ status: 'completed', threadId: 't1', response: 'Done.', toolResults: [] }),
        resume: jest.fn(),
      };
      const { textProcessor } = createHarness(agentClient);

      await textProcessor.processTextMessage('first', 42, { ...LOG, messageId: 1 });
      const result = await textProcessor.processTextMessage('second', 42, { ...LOG, messageId: 2 });
      expect(result.blocked).toBeUndefined();
      expect(agentClient.invoke).toHaveBeenCalledTimes(2);
    });
  });

  describe('interrupt flow: message → running → interrupt → waiting → resume → idle', () => {
    it('agent interrupt transitions gate to waiting, then text resume completes', async () => {
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted', threadId: 't1', response: 'Which one?',
          interrupt: { type: 'clarify', question: 'Which one?' }, toolResults: [],
        }),
        resume: jest.fn().mockResolvedValue({ status: 'completed', threadId: 't1', response: 'Done.', toolResults: [] }),
      };
      const { textProcessor, gateStore } = createHarness(agentClient);

      await textProcessor.processTextMessage('update task', 42, LOG);
      const key = buildConversationKey(42, 'telegram:42', 100);
      expect(await gateStore.getStatus(key)).toBe('waiting_for_clarification');

      const result = await textProcessor.processTextMessage('the dentist one', 42, { ...LOG, messageId: 2 });
      expect(result.response).toBe('Done.');
      expect(await gateStore.getStatus(key)).toBe('idle');
    });

    it('only first of (text + callback) proceeds, other gets rejection', async () => {
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted', threadId: 't1', response: 'Confirm?',
          interrupt: { type: 'confirm', summary: 'Delete?' }, toolResults: [],
        }),
        resume: jest.fn().mockResolvedValue({ status: 'completed', threadId: 't1', response: 'Done.', toolResults: [] }),
      };
      const { textProcessor, callbackHandler, gateStore } = createHarness(agentClient);

      await textProcessor.processTextMessage('delete all', 42, LOG);

      // Simulate race: text "yes" wins, callback loses
      const textResult = await textProcessor.processTextMessage('yes', 42, { ...LOG, messageId: 2 });
      expect(textResult.response).toBe('Done.');

      const ctx = makeCtx(`confirm:approve:t1`);
      await callbackHandler.handleCallbackQuery(ctx);
      expect(ctx.answerCbQuery).toHaveBeenCalledWith('This action has expired.');
    });
  });

  describe('concurrent message simulation', () => {
    it('5 messages fired simultaneously: only 1 acquires, 4 are blocked', async () => {
      const agentClient = {
        invoke: jest.fn().mockImplementation(() => new Promise(() => {})),
        resume: jest.fn(),
      };
      const { textProcessor } = createHarness(agentClient);

      // Fire first to acquire (don't await)
      textProcessor.processTextMessage('msg1', 42, { ...LOG, messageId: 1 });
      await new Promise((r) => setTimeout(r, 10));

      const results = await Promise.all([
        textProcessor.processTextMessage('msg2', 42, { ...LOG, messageId: 2 }),
        textProcessor.processTextMessage('msg3', 42, { ...LOG, messageId: 3 }),
        textProcessor.processTextMessage('msg4', 42, { ...LOG, messageId: 4 }),
        textProcessor.processTextMessage('msg5', 42, { ...LOG, messageId: 5 }),
      ]);

      expect(results.every((r) => r.blocked === true)).toBe(true);
      expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    });
  });

  describe('/cancel recovery', () => {
    it('/cancel during running state releases gate, next message succeeds', async () => {
      const agentClient = {
        invoke: jest.fn()
          .mockImplementationOnce(() => new Promise(() => {}))
          .mockResolvedValueOnce({ status: 'completed', threadId: 't2', response: 'Fresh.', toolResults: [] }),
        resume: jest.fn(),
      };
      const { textProcessor, gateStore, pendingStore } = createHarness(agentClient);

      textProcessor.processTextMessage('stuck', 42, LOG);
      await new Promise((r) => setTimeout(r, 10));

      const key = buildConversationKey(42, 'telegram:42', 100);
      await gateStore.release(key);
      await pendingStore.clear(key, 'failed').catch(() => {});

      const result = await textProcessor.processTextMessage('new', 42, { ...LOG, messageId: 2 });
      expect(result.response).toBe('Fresh.');
    });
  });

  describe('TTL auto-recovery', () => {
    it('gate stuck in running past TTL auto-expires on next check', async () => {
      const gateStore = new MemoryConversationGateStore();
      const key = buildConversationKey(42, 'telegram:42', 100);

      await gateStore.tryAcquire(key, 1); // 1ms TTL
      await new Promise((r) => setTimeout(r, 5));

      expect(await gateStore.getStatus(key)).toBe('idle');
    });
  });

  describe('fail-closed behavior', () => {
    it('gate store throwing on getSnapshot blocks the message without invoking the agent', async () => {
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({ status: 'completed', threadId: 't1', response: 'OK.', toolResults: [] }),
        resume: jest.fn(),
      };
      const pendingStore = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      gateStore.getSnapshot = jest.fn().mockRejectedValue(new Error('store down'));

      const textProcessor = new TextProcessorService(agentClient as any, pendingStore, gateStore);
      const result = await textProcessor.processTextMessage('hello', 42, LOG);
      expect(result.blocked).toBe(true);
      expect(result.response).toMatch(/still working/i);
      expect(agentClient.invoke).not.toHaveBeenCalled();
    });
  });

  describe('inconsistency detection', () => {
    it('gate=waiting + no pending record suppresses the message and preserves the gate', async () => {
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({ status: 'completed', threadId: 't1', response: 'Fresh.', toolResults: [] }),
        resume: jest.fn(),
      };
      const pendingStore = new MemoryPendingClarificationStore();
      const gateStore = new MemoryConversationGateStore();
      const key = buildConversationKey(42, 'telegram:42', 100);

      await gateStore.tryAcquire(key, 60000);
      await gateStore.transitionToWaiting(key, 60000);

      const textProcessor = new TextProcessorService(agentClient as any, pendingStore, gateStore);
      const result = await textProcessor.processTextMessage('hello', 42, LOG);
      expect(result).toEqual({ response: '', suppressed: true });
      expect(await gateStore.getStatus(key)).toBe('waiting_for_clarification');
      expect(agentClient.invoke).not.toHaveBeenCalled();
      expect(agentClient.resume).not.toHaveBeenCalled();

      await gateStore.release(key);
    });
  });
});
