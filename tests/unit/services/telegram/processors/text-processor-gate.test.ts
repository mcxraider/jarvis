import { TextProcessorService } from '../../../../../src/services/telegram/processors/text-processor.service';
import { MemoryPendingClarificationStore } from '../../../../../src/services/telegram/pending-clarification.store';
import { MemoryConversationGateStore } from '../../../../../src/services/telegram/conversation-gate.store';
import { buildConversationKey, mapTelegramUserId } from '../../../../../src/services/telegram/conversation-key';

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

    it('stores the active request around a fresh invoke and compare-clears it afterward', async () => {
      const gateStore = new MemoryConversationGateStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      let observedRequestId: string | undefined;
      const agentClient = mockAgentClient({
        invoke: jest.fn().mockImplementation(async () => {
          observedRequestId = await gateStore.getActiveRequestId(gateKey);
          return {
            status: 'completed',
            threadId: 'thread-1',
            response: 'Done.',
            toolResults: [],
          };
        }),
      });
      const service = createService(agentClient, undefined, gateStore);

      await service.processTextMessage(
        'hello',
        42,
        { chatId: 100, messageId: 1, requestId: 'request-fresh' },
      );

      expect(observedRequestId).toBe('request-fresh');
      expect(await gateStore.getActiveRequestId(gateKey)).toBeUndefined();
    });

    it('retains fresh invoke ownership when HTTP delivery is ambiguous', async () => {
      const gateStore = new MemoryConversationGateStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      const agentClient = mockAgentClient({
        invoke: jest.fn().mockResolvedValue({
          status: 'failed',
          delivery: 'ambiguous',
          threadId: '',
          response: 'This request may still be running. Use /cancel if you want to stop it.',
          toolResults: [],
          error: 'socket reset',
        }),
      });
      const service = createService(agentClient, undefined, gateStore);

      const result = await service.processTextMessage(
        'delete the duplicates',
        42,
        { chatId: 100, requestId: 'request-ambiguous-invoke' },
      );

      expect(result).toEqual(expect.objectContaining({
        delivery: 'ambiguous',
        response: expect.stringContaining('may still be running'),
      }));
      expect(await gateStore.getSnapshot(gateKey)).toEqual({
        status: 'running',
        requestId: 'request-ambiguous-invoke',
      });
      expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-ambiguous-invoke');
    });

    it('does not release or consume the buffer of a newer request when an older invoke settles', async () => {
      const gateStore = new MemoryConversationGateStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      let resolveInvoke!: (response: any) => void;
      const invoke = jest.fn().mockReturnValue(new Promise((resolve) => {
        resolveInvoke = resolve;
      }));
      const service = createService(mockAgentClient({ invoke }), undefined, gateStore);

      const oldRequest = service.processTextMessage(
        'old request',
        42,
        { chatId: 100, messageId: 1, requestId: 'request-old' },
      );
      while (invoke.mock.calls.length === 0) {
        await Promise.resolve();
      }

      expect(await gateStore.releaseIfActiveRequestId(gateKey, 'request-old')).toEqual({
        released: true,
        bufferedMessage: undefined,
      });
      expect(await gateStore.tryAcquire(gateKey, 60000)).toBe(true);
      await gateStore.setActiveRequestId(gateKey, 'request-new');
      await gateStore.setBufferedMessage(gateKey, 'new request buffer');

      resolveInvoke({
        status: 'completed',
        threadId: 'thread-old',
        response: 'Old response.',
        toolResults: [],
      });
      const result = await oldRequest;

      expect(result).toEqual({ response: '', suppressed: true });
      expect(await gateStore.getStatus(gateKey)).toBe('running');
      expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-new');
      expect(await gateStore.getAndClearBufferedMessage(gateKey)).toBe('new request buffer');
    });

    it('does not compare-clear a replacement generation that reuses the same backend request id', async () => {
      const gateStore = new MemoryConversationGateStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      const exactRelease = gateStore.releaseIfActiveRequestId.bind(gateStore);
      jest.spyOn(gateStore, 'releaseIfActiveRequestId').mockImplementationOnce(
        async (key, requestId) => {
          const released = await exactRelease(key, requestId);
          expect(released.released).toBe(true);
          expect(await gateStore.tryAcquire(key, 60_000, undefined, requestId)).toBe(true);
          return released;
        },
      );
      const service = createService(
        mockAgentClient(),
        undefined,
        gateStore,
      );

      const result = await service.processTextMessage(
        'same update replay',
        42,
        { chatId: 100, requestId: 'tg_update_123' },
      );

      expect(result.response).toBe('Done.');
      expect(await gateStore.getSnapshot(gateKey)).toEqual({
        status: 'running',
        requestId: 'tg_update_123',
      });
    });

    it('suppresses progress emitted after the producing request loses gate ownership', async () => {
      const gateStore = new MemoryConversationGateStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      const onProgress = jest.fn();
      const invoke = jest.fn().mockImplementation(async (_request, _context, progress) => {
        await gateStore.releaseIfActiveRequestId(gateKey, 'request-old');
        await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-new');
        await progress({ stage: 'tool_started', message: 'Stale progress' });
        return {
          status: 'completed',
          threadId: 'thread-old',
          response: 'Old response.',
          toolResults: [],
        };
      });
      const service = createService(mockAgentClient({ invoke }), undefined, gateStore);

      const result = await service.processTextMessage(
        'old request',
        42,
        { chatId: 100, requestId: 'request-old' },
        onProgress,
      );

      expect(onProgress).not.toHaveBeenCalled();
      expect(result).toEqual({ response: '', suppressed: true });
      expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-new');
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

    it('buffers rejected message for the captured running generation', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      // Manually acquire the gate
      await gateStore.tryAcquire('telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32), 60000);

      const agentClient = mockAgentClient();
      const service = createService(agentClient, pendingStore, gateStore);
      await service.processTextMessage(' \n buffered msg \t ', 42, { chatId: 100, messageId: 2 });

      // Verify the buffer was set by checking the gate store directly
      const key = 'telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32);
      const buffered = await gateStore.getAndClearBufferedMessage(key);
      expect(buffered).toBe('buffered msg');
    });

    it('does not buffer into a newer running generation after an ABA ownership change', async () => {
      const gateStore = new MemoryConversationGateStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      await gateStore.tryAcquire(gateKey, 60000, undefined, 'request-old');

      const originalSetBuffered = gateStore.setBufferedMessageIfActiveRequestId.bind(gateStore);
      gateStore.setBufferedMessageIfActiveRequestId = jest.fn().mockImplementation(
        async (key: string, expectedRequestId: string | undefined, message: string) => {
          expect(expectedRequestId).toBe('request-old');
          expect(await gateStore.releaseIfActiveRequestId(key, 'request-old')).toEqual({
            released: true,
            bufferedMessage: undefined,
          });
          expect(await gateStore.tryAcquire(key, 60000, undefined, 'request-new')).toBe(true);
          return originalSetBuffered(key, expectedRequestId, message);
        },
      );

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);
      const result = await service.processTextMessage(
        'blocked message',
        42,
        { chatId: 100, messageId: 2 },
      );

      expect(result).toEqual({
        response: "I'm still working on your previous request. Please wait.",
        blocked: true,
      });
      expect(agentClient.invoke).not.toHaveBeenCalled();
      expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-new');
      expect(await gateStore.getAndClearBufferedMessage(gateKey)).toBeUndefined();
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

    it('discards a pending interrupt saved after its waiting generation was cancelled', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      const originalSave = pendingStore.save.bind(pendingStore);
      let saveStarted!: () => void;
      let allowSave!: () => void;
      const saveEntered = new Promise<void>((resolve) => { saveStarted = resolve; });
      const saveReleased = new Promise<void>((resolve) => { allowSave = resolve; });
      pendingStore.save = jest.fn().mockImplementation(async (
        record: Parameters<MemoryPendingClarificationStore['save']>[0],
      ) => {
        saveStarted();
        await saveReleased;
        await originalSave(record);
      });
      const agentClient = mockAgentClient({
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-old',
          response: 'Which task?',
          interrupt: { type: 'clarify', question: 'Which task?' },
          toolResults: [],
        }),
      });
      const service = createService(agentClient, pendingStore, gateStore);

      const processing = service.processTextMessage(
        'update a task',
        42,
        { chatId: 100, requestId: 'request-old' },
      );
      await saveEntered;
      expect(await gateStore.releaseIfWaitingRequestId(gateKey, 'request-old')).toEqual({
        released: true,
        bufferedMessage: undefined,
      });
      expect(await gateStore.tryAcquire(gateKey, 60_000, undefined, 'request-new')).toBe(true);
      allowSave();

      const result = await processing;

      expect(result).toEqual({ response: '', suppressed: true });
      expect(await pendingStore.get(gateKey)).toBeUndefined();
      expect(await gateStore.getSnapshot(gateKey)).toEqual({
        status: 'running',
        requestId: 'request-new',
      });
    });

    it('retains the exact waiting generation when post-save verification storage fails', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      const originalGet = pendingStore.get.bind(pendingStore);
      let failReads = true;
      jest.spyOn(pendingStore, 'get').mockImplementation(async (...args) => {
        if (failReads) throw new Error('temporary pending read outage');
        return originalGet(...args);
      });
      jest.spyOn(pendingStore, 'clearIfMatches').mockRejectedValue(
        new Error('temporary pending cleanup outage'),
      );
      const service = createService(
        mockAgentClient({
          invoke: jest.fn().mockResolvedValue({
            status: 'interrupted',
            threadId: 'thread-1',
            response: 'Which task?',
            interrupt: { type: 'clarify', question: 'Which task?' },
            toolResults: [],
          }),
        }),
        pendingStore,
        gateStore,
      );

      const result = await service.processTextMessage(
        'update a task',
        42,
        { chatId: 100, requestId: 'request-interrupt' },
      );

      expect(result.interruptType).toBe('clarify');
      expect(await gateStore.getSnapshot(gateKey)).toEqual({
        status: 'waiting_for_clarification',
        requestId: 'request-interrupt',
      });

      failReads = false;
      expect((await originalGet(gateKey))?.requestId).toBe('request-interrupt');
      expect(await gateStore.releaseIfWaitingRequestId(
        gateKey,
        'request-interrupt',
      )).toEqual({ released: true, bufferedMessage: undefined });
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

    it('stores the new active request around a typed HITL resume', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      let observedRequestId: string | undefined;
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-1',
          response: 'Which task?',
          interrupt: { type: 'clarify', question: 'Which task?' },
          toolResults: [],
        }),
        resume: jest.fn().mockImplementation(async () => {
          observedRequestId = await gateStore.getActiveRequestId(gateKey);
          return {
            status: 'completed',
            threadId: 'thread-1',
            response: 'Done.',
            toolResults: [],
          };
        }),
      };
      const service = createService(agentClient, pendingStore, gateStore);
      await service.processTextMessage(
        'update task',
        42,
        { chatId: 100, messageId: 1, requestId: 'request-invoke' },
      );

      await service.processTextMessage(
        'the dentist one',
        42,
        { chatId: 100, messageId: 2, requestId: 'request-resume' },
      );

      expect(observedRequestId).toBe('request-resume');
      expect(await gateStore.getActiveRequestId(gateKey)).toBeUndefined();
    });

    it('retains resume ownership and the pending row when HTTP delivery is ambiguous', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-1',
          response: 'Which task?',
          interrupt: { type: 'clarify', question: 'Which task?' },
          toolResults: [],
        }),
        resume: jest.fn().mockResolvedValue({
          status: 'failed',
          delivery: 'ambiguous',
          threadId: 'thread-1',
          response: 'This request may still have completed. Use /cancel if you want to stop it.',
          toolResults: [],
          error: 'stream ended early',
        }),
      };
      const service = createService(agentClient, pendingStore, gateStore);
      await service.processTextMessage(
        'update task',
        42,
        { chatId: 100, requestId: 'request-invoke' },
      );

      const result = await service.processTextMessage(
        'the dentist task',
        42,
        { chatId: 100, requestId: 'request-ambiguous-resume' },
      );

      expect(result.delivery).toBe('ambiguous');
      expect(await gateStore.getSnapshot(gateKey)).toEqual({
        status: 'running',
        requestId: 'request-ambiguous-resume',
      });
      expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-ambiguous-resume');
      expect(await pendingStore.get(gateKey)).toEqual(expect.objectContaining({
        threadId: 'thread-1',
        requestId: 'request-invoke',
        status: 'pending',
      }));
    });

    it('does not release a newer request when an older typed HITL resume settles', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      let resolveResume!: (response: any) => void;
      const resume = jest.fn().mockReturnValue(new Promise((resolve) => {
        resolveResume = resolve;
      }));
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-1',
          response: 'Which task?',
          interrupt: { type: 'clarify', question: 'Which task?' },
          toolResults: [],
        }),
        resume,
      };
      const service = createService(agentClient, pendingStore, gateStore);
      await service.processTextMessage(
        'update task',
        42,
        { chatId: 100, messageId: 1, requestId: 'request-invoke' },
      );

      const oldResume = service.processTextMessage(
        'the dentist one',
        42,
        { chatId: 100, messageId: 2, requestId: 'request-old-resume' },
      );
      while (resume.mock.calls.length === 0) {
        await Promise.resolve();
      }

      expect(await gateStore.releaseIfActiveRequestId(gateKey, 'request-old-resume')).toEqual({
        released: true,
        bufferedMessage: undefined,
      });
      expect(await gateStore.tryAcquire(gateKey, 60000)).toBe(true);
      await gateStore.setActiveRequestId(gateKey, 'request-new');

      resolveResume({
        status: 'completed',
        threadId: 'thread-1',
        response: 'Old resume response.',
        toolResults: [],
      });
      const result = await oldResume;

      expect(result).toEqual({ response: '', suppressed: true });
      expect(await gateStore.getStatus(gateKey)).toBe('running');
      expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-new');
    });

    it('does not resume when the waiting generation changes after snapshot lookup', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      const agentClient = {
        invoke: jest.fn().mockResolvedValue({
          status: 'interrupted',
          threadId: 'thread-old',
          response: 'Which task?',
          interrupt: { type: 'clarify' },
          toolResults: [],
        }),
        resume: jest.fn(),
      };
      const service = createService(agentClient, pendingStore, gateStore);
      await service.processTextMessage(
        'update task',
        42,
        { chatId: 100, requestId: 'waiting-old' },
      );
      const getPending = pendingStore.get.bind(pendingStore);
      pendingStore.get = jest.fn().mockImplementationOnce(async (key) => {
        const oldPending = await getPending(key);
        await gateStore.releaseIfWaitingRequestId(gateKey, 'waiting-old');
        await gateStore.tryAcquire(gateKey, 60000, undefined, 'waiting-new');
        await gateStore.transitionToWaitingIfActiveRequestId(gateKey, 'waiting-new', 60000);
        await pendingStore.save({
          ...oldPending!,
          threadId: 'thread-new',
          requestId: 'waiting-new',
        });
        return oldPending;
      });

      const result = await service.processTextMessage(
        'the dentist task',
        42,
        { chatId: 100, requestId: 'resume-old' },
      );

      expect(result.response).toMatch(/already processing/i);
      expect(agentClient.resume).not.toHaveBeenCalled();
      expect(await gateStore.getSnapshot(gateKey)).toEqual({
        status: 'waiting_for_clarification',
        requestId: 'waiting-new',
      });
      expect((await getPending(gateKey))?.requestId).toBe('waiting-new');
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

  describe('fail-closed ownership binding', () => {
    it('does not invoke when the gate snapshot cannot be read', async () => {
      const gateStore = new MemoryConversationGateStore();
      gateStore.getSnapshot = jest.fn().mockRejectedValue(new Error('store down'));

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      const result = await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });
      expect(result.blocked).toBe(true);
      expect(agentClient.invoke).not.toHaveBeenCalled();
    });

    it('does not invoke when tryAcquire throws', async () => {
      const gateStore = new MemoryConversationGateStore();
      gateStore.tryAcquire = jest.fn().mockRejectedValue(new Error('store down'));

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      const result = await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });
      expect(result.blocked).toBe(true);
      expect(agentClient.invoke).not.toHaveBeenCalled();
    });
  });

  describe('gatePreAcquired option', () => {
    it('skips gate check and acquisition when gatePreAcquired=true', async () => {
      const gateStore = new MemoryConversationGateStore();
      const spyGetStatus = jest.spyOn(gateStore, 'getStatus');

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);
      const key = buildConversationKey(42, mapTelegramUserId(42), 100);
      await gateStore.tryAcquire(key, 60000, 100, 'tg_test');
      const spyTryAcquire = jest.spyOn(gateStore, 'tryAcquire');

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
      await gateStore.tryAcquire(key, 60000, undefined, 'tg_test');

      await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 }, undefined, { gatePreAcquired: true });

      // The release happens inside releaseGateWithBuffer — gate should be idle now
      expect(await gateStore.getStatus(key)).toBe('idle');
    });

    it('fails closed when a pre-acquired gate was rebound before invoke', async () => {
      const gateStore = new MemoryConversationGateStore();
      const gateKey = buildConversationKey(42, mapTelegramUserId(42), 100);
      await gateStore.tryAcquire(gateKey, 60000, 100, 'request-new');
      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      const result = await service.processTextMessage(
        'old request',
        42,
        { chatId: 100, messageId: 1, requestId: 'request-old' },
        undefined,
        { gatePreAcquired: true },
      );

      expect(result.suppressed).toBe(true);
      expect(agentClient.invoke).not.toHaveBeenCalled();
      expect(await gateStore.getActiveRequestId(gateKey)).toBe('request-new');
    });
  });

  describe('inconsistency detection', () => {
    it('when gate=waiting but pending store returns undefined, retains the transient wait', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const key = 'telegram-chat:' + require('crypto').createHash('sha256').update('100:42').digest('hex').slice(0, 32);

      // Manually set gate to waiting without a pending record
      await gateStore.tryAcquire(key, 60000, undefined, 'waiting-owner');
      await gateStore.transitionToWaitingIfActiveRequestId(key, 'waiting-owner', 60000);

      const agentClient = mockAgentClient();
      const service = createService(agentClient, pendingStore, gateStore);

      const result = await service.processTextMessage('hello', 42, { chatId: 100, messageId: 1 });
      expect(result.suppressed).toBe(true);
      expect(agentClient.invoke).not.toHaveBeenCalled();
    });
  });

  describe('forceFresh (/new)', () => {
    function keyFor(userId: number, chatId: number): string {
      return buildConversationKey(userId, mapTelegramUserId(userId), chatId);
    }

    async function seedWaiting(
      gateStore: MemoryConversationGateStore,
      pendingStore: MemoryPendingClarificationStore,
      key: string,
    ): Promise<void> {
      await gateStore.tryAcquire(key, 60000, undefined, 'waiting-owner');
      await gateStore.transitionToWaitingIfActiveRequestId(key, 'waiting-owner', 60000);
      const now = Date.now();
      await pendingStore.save({
        pendingKey: key,
        threadId: 'old-thread',
        question: 'Confirm?',
        userId: 'user-42',
        requestId: 'waiting-owner',
        interruptType: 'confirm',
        promptMessageId: 700,
        status: 'pending',
        createdAt: now,
        updatedAt: now,
        expiresAt: now + 60000,
      });
    }

    it('abandons a pending confirm and invokes a fresh thread', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const key = keyFor(42, 100);
      await seedWaiting(gateStore, pendingStore, key);

      const agentClient = mockAgentClient();
      const service = createService(agentClient, pendingStore, gateStore);

      const result = await service.processTextMessage(
        'do something else',
        42,
        { chatId: 100, messageId: 9 },
        undefined,
        { forceFresh: true },
      );

      expect(agentClient.invoke).toHaveBeenCalledTimes(1);
      expect(agentClient.resume).not.toHaveBeenCalled();
      expect(result.response).toBe('Done.');
      // Pending record abandoned (memory store deletes on clear).
      expect(await pendingStore.get(key)).toBeUndefined();
    });

    it('returns superseded prompt metadata when a competitor wins the fresh acquire', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const key = keyFor(42, 100);
      await seedWaiting(gateStore, pendingStore, key);
      const exactClear = pendingStore.clearIfMatches.bind(pendingStore);
      jest.spyOn(pendingStore, 'clearIfMatches').mockImplementationOnce(async (...args) => {
        const cleared = await exactClear(...args);
        await gateStore.tryAcquire(key, 60_000, undefined, 'request-competitor');
        return cleared;
      });
      const agentClient = mockAgentClient();
      const service = createService(agentClient, pendingStore, gateStore);

      const result = await service.processTextMessage(
        'do something else',
        42,
        { chatId: 100, requestId: 'request-new' },
        undefined,
        { forceFresh: true },
      );

      expect(result).toEqual(expect.objectContaining({
        blocked: true,
        resolvedPendingPause: true,
        consumedInterruptType: 'confirm',
        consumedPromptMessageId: 700,
      }));
      expect(agentClient.invoke).not.toHaveBeenCalled();
      expect(await gateStore.getSnapshot(key)).toEqual({
        status: 'running',
        requestId: 'request-competitor',
      });
    });

    it('refuses to start fresh while the agent is running', async () => {
      const gateStore = new MemoryConversationGateStore();
      const key = keyFor(42, 100);
      await gateStore.tryAcquire(key, 60000); // status: running

      const agentClient = mockAgentClient();
      const service = createService(agentClient, undefined, gateStore);

      const result = await service.processTextMessage(
        'new thing',
        42,
        { chatId: 100, messageId: 9 },
        undefined,
        { forceFresh: true },
      );

      expect(result.blocked).toBe(true);
      expect(result.response).toMatch(/still finishing/i);
      expect(agentClient.invoke).not.toHaveBeenCalled();
    });

    it('abandonConversation returns abandoned and supersedes the pending record', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const key = keyFor(42, 100);
      await seedWaiting(gateStore, pendingStore, key);

      const service = createService(mockAgentClient(), pendingStore, gateStore);
      const outcome = await service.abandonConversation(42, { chatId: 100 });

      expect(outcome).toBe('abandoned');
      expect(await gateStore.getStatus(key)).toBe('idle');
      expect(await pendingStore.get(key)).toBeUndefined();
    });

    it('abandonConversation returns running when the agent is mid-flight', async () => {
      const gateStore = new MemoryConversationGateStore();
      const key = keyFor(42, 100);
      await gateStore.tryAcquire(key, 60000);

      const service = createService(mockAgentClient(), undefined, gateStore);
      expect(await service.abandonConversation(42, { chatId: 100 })).toBe('running');
    });

    it('retains a waiting gate when its pending snapshot generation does not match', async () => {
      const gateStore = new MemoryConversationGateStore();
      const pendingStore = new MemoryPendingClarificationStore();
      const key = keyFor(42, 100);
      await gateStore.tryAcquire(key, 60000, undefined, 'waiting-new');
      await gateStore.transitionToWaitingIfActiveRequestId(key, 'waiting-new', 60000);
      const now = Date.now();
      await pendingStore.save({
        pendingKey: key,
        threadId: 'thread-old',
        question: 'Old question?',
        userId: 'telegram:42',
        requestId: 'waiting-old',
        status: 'pending',
        createdAt: now,
        updatedAt: now,
        expiresAt: now + 60000,
      });
      const service = createService(mockAgentClient(), pendingStore, gateStore);

      expect(await service.abandonConversation(42, { chatId: 100 })).toBe('running');
      expect(await gateStore.getRequestId(key)).toBe('waiting-new');
      expect((await pendingStore.get(key))?.requestId).toBe('waiting-old');
    });

    it('abandonConversation returns idle when nothing is pending', async () => {
      const service = createService(mockAgentClient());
      expect(await service.abandonConversation(42, { chatId: 100 })).toBe('idle');
    });
  });
});
