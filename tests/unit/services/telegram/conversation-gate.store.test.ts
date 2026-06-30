import { MemoryConversationGateStore } from '../../../../src/services/telegram/conversation-gate.store';

describe('MemoryConversationGateStore', () => {
  let store: MemoryConversationGateStore;

  beforeEach(() => {
    store = new MemoryConversationGateStore();
  });

  describe('tryAcquire', () => {
    it('succeeds when no record exists', async () => {
      expect(await store.tryAcquire('key1', 5000)).toBe(true);
      expect(await store.getStatus('key1')).toBe('running');
    });

    it('fails when status is running and not expired', async () => {
      await store.tryAcquire('key1', 60000);
      expect(await store.tryAcquire('key1', 60000)).toBe(false);
    });

    it('succeeds when record exists but is expired', async () => {
      await store.tryAcquire('key1', 1);
      // Wait for expiry
      await new Promise((r) => setTimeout(r, 5));
      expect(await store.tryAcquire('key1', 60000)).toBe(true);
    });

    it('succeeds when status is idle', async () => {
      await store.tryAcquire('key1', 60000);
      await store.release('key1');
      // After release there's no record, which also means idle
      expect(await store.tryAcquire('key1', 60000)).toBe(true);
    });
  });

  describe('getStatus', () => {
    it('returns idle for unknown key', async () => {
      expect(await store.getStatus('unknown')).toBe('idle');
    });

    it('returns idle for expired record (auto-cleanup)', async () => {
      await store.tryAcquire('key1', 1);
      await new Promise((r) => setTimeout(r, 5));
      expect(await store.getStatus('key1')).toBe('idle');
    });

    it('returns correct status for active record', async () => {
      await store.tryAcquire('key1', 60000);
      expect(await store.getStatus('key1')).toBe('running');
    });
  });

  describe('release', () => {
    it('sets status back to idle', async () => {
      await store.tryAcquire('key1', 60000);
      await store.release('key1');
      expect(await store.getStatus('key1')).toBe('idle');
    });

    it('does not throw on unknown key', async () => {
      await expect(store.release('nonexistent')).resolves.toBeUndefined();
    });
  });

  describe('transitionToWaiting', () => {
    it('changes running to waiting_for_clarification', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 60000);
      expect(await store.getStatus('key1')).toBe('waiting_for_clarification');
    });

    it('is no-op when not running', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 60000);
      // Now it's waiting — transitioning again should be no-op
      await store.transitionToWaiting('key1', 60000);
      expect(await store.getStatus('key1')).toBe('waiting_for_clarification');
    });

    it('is no-op on unknown key', async () => {
      await expect(store.transitionToWaiting('unknown', 60000)).resolves.toBeUndefined();
    });
  });

  describe('transitionToRunning', () => {
    it('succeeds from waiting_for_clarification', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 60000);
      expect(await store.transitionToRunning('key1', 60000)).toBe(true);
      expect(await store.getStatus('key1')).toBe('running');
    });

    it('fails from idle (no record)', async () => {
      expect(await store.transitionToRunning('key1', 60000)).toBe(false);
    });

    it('fails from running', async () => {
      await store.tryAcquire('key1', 60000);
      expect(await store.transitionToRunning('key1', 60000)).toBe(false);
    });

    it('fails when waiting but expired', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 1);
      await new Promise((r) => setTimeout(r, 5));
      expect(await store.transitionToRunning('key1', 60000)).toBe(false);
    });

    it('is atomic — only first caller wins', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 60000);

      const result1 = store.transitionToRunning('key1', 60000);
      const result2 = store.transitionToRunning('key1', 60000);

      const [r1, r2] = await Promise.all([result1, result2]);
      expect([r1, r2].filter(Boolean)).toHaveLength(1);
    });
  });

  describe('message buffer', () => {
    it('stores and retrieves a message', async () => {
      await store.tryAcquire('key1', 60000);
      await store.setBufferedMessage('key1', 'hello');
      expect(await store.getAndClearBufferedMessage('key1')).toBe('hello');
    });

    it('caps message at 4096 chars', async () => {
      await store.tryAcquire('key1', 60000);
      const longMsg = 'x'.repeat(5000);
      await store.setBufferedMessage('key1', longMsg);
      const retrieved = await store.getAndClearBufferedMessage('key1');
      expect(retrieved).toHaveLength(4096);
    });

    it('returns undefined when no buffer', async () => {
      await store.tryAcquire('key1', 60000);
      expect(await store.getAndClearBufferedMessage('key1')).toBeUndefined();
    });

    it('clears on retrieval (second call returns undefined)', async () => {
      await store.tryAcquire('key1', 60000);
      await store.setBufferedMessage('key1', 'hello');
      await store.getAndClearBufferedMessage('key1');
      expect(await store.getAndClearBufferedMessage('key1')).toBeUndefined();
    });

    it('buffer is cleared when gate is re-acquired', async () => {
      await store.tryAcquire('key1', 1);
      await store.setBufferedMessage('key1', 'old message');
      await new Promise((r) => setTimeout(r, 5));
      await store.tryAcquire('key1', 60000);
      expect(await store.getAndClearBufferedMessage('key1')).toBeUndefined();
    });

    it('does nothing when no record exists', async () => {
      await expect(store.setBufferedMessage('unknown', 'msg')).resolves.toBeUndefined();
      expect(await store.getAndClearBufferedMessage('unknown')).toBeUndefined();
    });
  });

  describe('TTL behavior', () => {
    it('running gate expires after TTL', async () => {
      await store.tryAcquire('key1', 1);
      await new Promise((r) => setTimeout(r, 5));
      expect(await store.getStatus('key1')).toBe('idle');
    });

    it('waiting gate expires after TTL', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 1);
      await new Promise((r) => setTimeout(r, 5));
      expect(await store.getStatus('key1')).toBe('idle');
    });

    it('tryAcquire reclaims expired running gate', async () => {
      await store.tryAcquire('key1', 1);
      await new Promise((r) => setTimeout(r, 5));
      expect(await store.tryAcquire('key1', 60000)).toBe(true);
    });

    it('tryAcquire reclaims expired waiting gate', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 1);
      await new Promise((r) => setTimeout(r, 5));
      expect(await store.tryAcquire('key1', 60000)).toBe(true);
    });
  });
});
