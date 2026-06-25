import { TextProcessorService } from '../../../../../src/services/telegram/processors/text-processor.service';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';

function createService(
  agentClient: unknown,
  store = new MemoryPendingClarificationStore(),
  gateStore = new MemoryConversationGateStore(),
): TextProcessorService {
  return new TextProcessorService(agentClient as any, store, gateStore);
}

function mockAgentClient(overrides: Partial<{ invoke: jest.Mock; resume: jest.Mock }> = {}) {
  return {
    invoke: jest.fn().mockResolvedValue({
      status: 'completed',
      threadId: 'thread-1',
      response: 'Done.',
      toolResults: [],
    }),
    resume: jest.fn().mockResolvedValue({
      status: 'completed',
      threadId: 'thread-1',
      response: 'Resumed.',
      toolResults: [],
    }),
    ...overrides,
  };
}

describe('TextProcessorService — conversation gate integration', () => {
  describe('gate acquisition on new messages', () => {
    it('acquires gate and calls invoke() when gate is idle', async () => {
      const agentClient = mockAgentClient();
      const gateStore = new MemoryConversationGateStore();
      const service = createService(agentClient, undefined, gateStore);

      await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });

      expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    });

    it('returns blocked response when gate is running', async () => {
      const gateStore = new MemoryConversationGateStore();
      const agentClient = mockAgentClient({
        invoke: jest.fn().mockImplementation(() => new Promise(() => {})), // never resolves
      });
      const service = createService(agentClient, undefined, gateStore);

      // Start a request that won't complete
      service.processTextMessage('first', 42, { chatId: 100, messageId: 1 });
      // Wait a tick for the gate to be acquired
      await new Promise((r) => setTimeout(r, 10));

      // Second request should be blocked
      const fastClient = mockAgentClient();
      const service2 = createService(fastClient, undefined, gateStore);
      const result = await service2.processTextMessage('second', 42, { chatId: 100, messageId: 2 });

      expect(result.blocked).toBe(true);
      expect(result.response).toMatch(/still working/i);
      expect(fastClient.invoke).not.toHaveBeenCalled();
    });

    it('buffers rejected message via setBufferedMessage()', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      // Manually acquire the gate
      await gateStore.tryAcquire('telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32), 60000);

      const agentClient = mockAgentClient();
      const service = createService(agentClient, pendingStore, gateStore);
      await service.processTextMessage('buffered msg', 42, { chatId: 100, messageId: 2 });

      // Verify the buffer was set by checking the gate store directly
      const key = 'telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32);
      const buffered = await gateStore.getAndClearBufferedMessage(key);
      expect(buffered).toBe('buffered msg');
    });

    it('releases gate after successful completion', async () => {
      const gateStore = new MemoryConversationGateStore();
      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });

      // Gate should now be idle — a second message should also succeed
      const result = await service.processTextMessage('world', 42, { chatId: 100, messageId: 2 });
      expect(result.blocked).toBeUndefined();
      expect(agentClient.invoke).toHaveBeenCalledTimes(2);
    });

    it('releases gate on unexpected errors', async () => {
      const gateStore = new MemoryConversationGateStore();
      const agentClient = mockAgentClient({
        invoke: jest.fn().mockRejectedValue(new Error('network failure')),
      });
      const service = createService(agentClient, undefined, gateStore);

      await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });

      // Should be able to acquire again
      const key = 'telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32);
      expect(await gateStore.getStatus(key)).toBe('idle');
    });
  });

  describe('gate transitions on interrupts', () => {
    it('transitions to waiting_for_clarification when agent interrupts', async () => {
      const gateStore = new MemoryConversationGateStore();
      const agentClient = mockAgentClient({
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-1',
          response: 'Which task?',
          interrupt: { type: 'clarify', question: 'Which task?' },
          toolResults: [],
        }),
      });
      const service = createService(agentClient, undefined, gateStore);

      await service.processTextMessage('update task', 42, { chatId: 100, messageId: 1 });

      const key = 'telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32);
      expect(await gateStore.getStatus(key)).toBe('waiting_for_clarification');
    });
  });

  describe('resume path (pending clarification)', () => {
    it('calls transitionToRunning BEFORE calling resume()', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const callOrder: string[] = [];

      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-1',
          response: 'Which task?',
          interrupt: { type: 'clarify', question: 'Which task?' },
          toolResults: [],
        }),
        resume: jest.fn().mockImplementation(() => {
          callOrder.push('resume');
          return Promise.resolve({
            status: 'completed',
            threadId: 'thread-1',
            response: 'Done.',
            toolResults: [],
          });
        }),
      };

      const origTransition = gateStore.transitionToRunning.bind(gateStore);
      gateStore.transitionToRunning = jest.fn().mockImplementation((...args) => {
        callOrder.push('transitionToRunning');
        return origTransition(...(args as [string, number]));
      });

      const service = createService(agentClient, pendingStore, gateStore);

      await service.processTextMessage('update task', 42, { chatId: 100, messageId: 1 });
      await service.processTextMessage('the dentist one', 42, { chatId: 100, messageId: 2 });

      expect(callOrder).toEqual(['transitionToRunning', 'resume']);
    });

    it('returns "already processing" when transitionToRunning returns false', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-1',
          response: 'Which task?',
          interrupt: { type: 'clarify', question: 'Which task?' },
          toolResults: [],
        }),
        resume: jest.fn(),
      };

      const service = createService(agentClient, pendingStore, gateStore);
      await service.processTextMessage('update task', 42, { chatId: 100, messageId: 1 });

      // Stub transitionToRunning to return false (simulate another path winning the race)
      gateStore.transitionToRunning = jest.fn().mockResolvedValue(false);

      const result = await service.processTextMessage('the dentist one', 42, { chatId: 100, messageId: 2 });
      expect(result.response).toMatch(/already processing/i);
      expect(agentClient.resume).not.toHaveBeenCalled();
    });
  });

  describe('fail-open behavior', () => {
    it('proceeds without gate when getStatus() throws', async () => {
      const gateStore = new MemoryConversationGateStore();
      gateStore.getStatus = jest.fn().mockRejectedValue(new Error('store down'));
      gateStore.tryAcquire = jest.fn().mockResolvedValue(true);

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      const result = await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });
      expect(result.response).toBe('Done.');
      expect(agentClient.invoke).toHaveBeenCalled();
    });

    it('proceeds without gate when tryAcquire() throws', async () => {
      const gateStore = new MemoryConversationGateStore();
      gateStore.tryAcquire = jest.fn().mockRejectedValue(new Error('store down'));

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      const result = await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });
      expect(result.response).toBe('Done.');
    });
  });

  describe('gatePreAcquired option', () => {
    it('skips gate check and acquisition when gatePreAcquired=true', async () => {
      const gateStore = new MemoryConversationGateStore();
      const spyGetStatus = jest.spyOn(gateStore, 'getStatus');
      const spyTryAcquire = jest.spyOn(gateStore, 'tryAcquire');

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 }, undefined, { gatePreAcquired: true });

      expect(spyGetStatus).not.toHaveBeenCalled();
      expect(spyTryAcquire).not.toHaveBeenCalled();
      expect(agentClient.invoke).toHaveBeenCalled();
    });

    it('still releases gate on completion when gatePreAcquired=true', async () => {
      const gateStore = new MemoryConversationGateStore();
      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      // Pre-acquire the gate
      const key = 'telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32);
      await gateStore.tryAcquire(key, 60000);

      await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 }, undefined, { gatePreAcquired: true });

      // The release happens inside releaseGateWithBuffer — gate should be idle now
      expect(await gateStore.getStatus(key)).toBe('idle');
    });
  });

  describe('inconsistency detection', () => {
    it('when gate=waiting but pending store returns undefined → releases gate and invokes fresh', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const key = 'telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32);

      // Manually set gate to waiting without a pending record
      await gateStore.tryAcquire(key, 60000);
      await gateStore.transitionToWaiting(key, 60000);

      const agentClient = mockAgentClient();
      const service = createService(agentClient, pendingStore, gateStore);

      const result = await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });
      expect(result.response).toBe('Done.');
      expect(agentClient.invoke).toHaveBeenCalledTimes(1);
    });
  });
});
