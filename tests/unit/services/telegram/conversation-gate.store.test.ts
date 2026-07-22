import {
  MemoryConversationGateStore,
  PostgresConversationGateStore,
} from '../../../../src/services/telegram/conversation-gate.store';

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

    it('compare-transitions only the observed waiting generation while binding the new request', async () => {
      await store.tryAcquire('key1', 60000, undefined, 'waiting-new');
      await store.transitionToWaitingIfActiveRequestId('key1', 'waiting-new', 60000);

      expect(await store.transitionToRunning('key1', 60000, 'resume-old', 'waiting-old')).toBe(false);
      expect(await store.getSnapshot('key1')).toEqual({
        status: 'waiting_for_clarification',
        requestId: 'waiting-new',
      });

      expect(await store.transitionToRunning('key1', 60000, 'resume-new', 'waiting-new')).toBe(true);
      expect(await store.getSnapshot('key1')).toEqual({
        status: 'running',
        requestId: 'resume-new',
      });
    });

    it('distinguishes a legacy waiting NULL generation from idle and can CAS it', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 60000);

      expect(await store.getSnapshot('key1')).toEqual({
        status: 'waiting_for_clarification',
        requestId: undefined,
      });
      expect(await store.transitionToRunning('key1', 60000, 'resume-new', undefined)).toBe(true);
      expect(await store.getActiveRequestId('key1')).toBe('resume-new');
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

  describe('active request ownership', () => {
    it('stores the active request only for a running gate', async () => {
      await store.setActiveRequestId('key1', 'request-before-acquire');
      expect(await store.getActiveRequestId('key1')).toBeUndefined();

      await store.tryAcquire('key1', 60000);
      await store.setActiveRequestId('key1', 'request-1');
      expect(await store.getActiveRequestId('key1')).toBe('request-1');
    });

    it('binds ownership atomically on acquire and refuses a stale overwrite', async () => {
      await store.tryAcquire('key1', 60000, undefined, 'request-new');
      expect(await store.getActiveRequestId('key1')).toBe('request-new');

      await expect(store.setActiveRequestId('key1', 'request-old')).resolves.toBe(false);
      expect(await store.getActiveRequestId('key1')).toBe('request-new');
      await expect(store.setActiveRequestId('key1', 'request-new')).resolves.toBe(true);
    });

    it('compare-clears only the request that still owns the gate', async () => {
      await store.tryAcquire('key1', 60000);
      await store.setActiveRequestId('key1', 'request-2');

      await store.clearActiveRequestId('key1', 'request-1');
      expect(await store.getActiveRequestId('key1')).toBe('request-2');

      await store.clearActiveRequestId('key1', 'request-2');
      expect(await store.getActiveRequestId('key1')).toBeUndefined();
    });

    it('clears the active request when the gate waits or releases', async () => {
      await store.tryAcquire('key1', 60000);
      await store.setActiveRequestId('key1', 'request-1');
      await store.transitionToWaiting('key1', 60000);
      expect(await store.getActiveRequestId('key1')).toBeUndefined();

      await store.transitionToRunning('key1', 60000);
      await store.setActiveRequestId('key1', 'request-2');
      await store.release('key1');
      expect(await store.getActiveRequestId('key1')).toBeUndefined();
    });

    it('does not return an active request after gate expiry', async () => {
      await store.tryAcquire('key1', 1);
      await store.setActiveRequestId('key1', 'request-1');
      await new Promise((resolve) => setTimeout(resolve, 5));
      expect(await store.getActiveRequestId('key1')).toBeUndefined();
    });

    it('atomically releases and returns the buffer only for the current owner', async () => {
      await store.tryAcquire('key1', 60000);
      await store.setActiveRequestId('key1', 'request-2');
      await store.setBufferedMessage('key1', 'new owner buffer');

      await expect(store.releaseIfActiveRequestId('key1', 'request-1')).resolves.toEqual({
        released: false,
      });
      expect(await store.getStatus('key1')).toBe('running');
      expect(await store.getActiveRequestId('key1')).toBe('request-2');

      await expect(store.releaseIfActiveRequestId('key1', 'request-2')).resolves.toEqual({
        released: true,
        bufferedMessage: 'new owner buffer',
      });
      expect(await store.getStatus('key1')).toBe('idle');
    });

    it('does not let a stale owner transition a newer running request to waiting', async () => {
      await store.tryAcquire('key1', 60000);
      await store.setActiveRequestId('key1', 'request-2');

      await expect(
        store.transitionToWaitingIfActiveRequestId('key1', 'request-1', 60000),
      ).resolves.toBe(false);
      expect(await store.getStatus('key1')).toBe('running');
      expect(await store.getActiveRequestId('key1')).toBe('request-2');

      await expect(
        store.transitionToWaitingIfActiveRequestId('key1', 'request-2', 60000),
      ).resolves.toBe(true);
      expect(await store.getStatus('key1')).toBe('waiting_for_clarification');
      expect(await store.getActiveRequestId('key1')).toBeUndefined();
    });

    it('atomically restores the prior waiting generation after a reserved resume fails', async () => {
      await store.tryAcquire('key1', 60000, undefined, 'waiting-owner');
      await store.transitionToWaitingIfActiveRequestId('key1', 'waiting-owner', 60000);
      await store.transitionToRunning('key1', 60000, 'resume-owner', 'waiting-owner');

      await expect(
        store.transitionToWaitingIfActiveRequestId(
          'key1',
          'resume-owner',
          60000,
          'waiting-owner',
        ),
      ).resolves.toBe(true);
      expect(await store.getSnapshot('key1')).toEqual({
        status: 'waiting_for_clarification',
        requestId: 'waiting-owner',
      });
    });

    it('can explicitly restore a legacy NULL waiting generation', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 60000);
      await store.transitionToRunning('key1', 60000, 'cancel-owner', undefined);

      await expect(
        store.transitionToWaitingIfActiveRequestId('key1', 'cancel-owner', 60000, null),
      ).resolves.toBe(true);
      expect(await store.getSnapshot('key1')).toEqual({
        status: 'waiting_for_clarification',
        requestId: undefined,
      });
    });

    it('releases a waiting gate only for its retained generation token', async () => {
      await store.tryAcquire('key1', 60000, undefined, 'request-new');
      await store.transitionToWaitingIfActiveRequestId('key1', 'request-new', 60000);

      await expect(store.releaseIfWaitingRequestId('key1', 'request-old')).resolves.toEqual({
        released: false,
      });
      expect(await store.getStatus('key1')).toBe('waiting_for_clarification');
      expect(await store.getRequestId('key1')).toBe('request-new');

      await expect(store.releaseIfWaitingRequestId('key1', 'request-new')).resolves.toEqual({
        released: true,
        bufferedMessage: undefined,
      });
      expect(await store.getStatus('key1')).toBe('idle');
    });

    it('releases a legacy waiting gate with an observed undefined generation', async () => {
      await store.tryAcquire('key1', 60000);
      await store.transitionToWaiting('key1', 60000);

      await expect(store.releaseIfWaitingRequestId('key1', undefined)).resolves.toEqual({
        released: true,
        bufferedMessage: undefined,
      });
      expect(await store.getStatus('key1')).toBe('idle');
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

    it('fires read-discovered expiry cleanup exactly once with the expired generation', async () => {
      jest.useFakeTimers();
      try {
        jest.setSystemTime(new Date('2026-07-16T00:00:00.000Z'));
        const onExpiry = jest.fn();
        store.setOnExpiry(onExpiry);
        await store.tryAcquire('key1', 1000, 123, 'request-expired');

        // Move wall-clock time past the deadline without running the scheduled timer.
        jest.setSystemTime(new Date('2026-07-16T00:00:01.001Z'));
        await expect(store.getSnapshot('key1')).resolves.toEqual({ status: 'idle' });
        await expect(store.getSnapshot('key1')).resolves.toEqual({ status: 'idle' });
        await jest.runOnlyPendingTimersAsync();

        expect(onExpiry).toHaveBeenCalledTimes(1);
        expect(onExpiry).toHaveBeenCalledWith('key1', 123, 'request-expired');
      } finally {
        jest.useRealTimers();
      }
    });
  });
});

describe('MemoryConversationGateStore — expiry seizure race', () => {
  let store: MemoryConversationGateStore;

  beforeEach(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-07-20T00:00:00.000Z'));
    store = new MemoryConversationGateStore();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('turn B seizes an expired gate from turn A', async () => {
    await store.tryAcquire('key1', 1000, undefined, 'turn-a');
    expect(await store.getActiveRequestId('key1')).toBe('turn-a');

    jest.setSystemTime(new Date('2026-07-20T00:00:01.001Z'));

    expect(await store.tryAcquire('key1', 60000, undefined, 'turn-b')).toBe(true);
    expect(await store.getActiveRequestId('key1')).toBe('turn-b');
  });

  it('stale owner releaseIfActiveRequestId is rejected after seizure', async () => {
    await store.tryAcquire('key1', 1000, undefined, 'turn-a');
    jest.setSystemTime(new Date('2026-07-20T00:00:01.001Z'));
    await store.tryAcquire('key1', 60000, undefined, 'turn-b');

    const result = await store.releaseIfActiveRequestId('key1', 'turn-a');
    expect(result).toEqual({ released: false });

    expect(await store.getStatus('key1')).toBe('running');
    expect(await store.getActiveRequestId('key1')).toBe('turn-b');
  });

  it('stale owner transitionToWaitingIfActiveRequestId is rejected after seizure', async () => {
    await store.tryAcquire('key1', 1000, undefined, 'turn-a');
    jest.setSystemTime(new Date('2026-07-20T00:00:01.001Z'));
    await store.tryAcquire('key1', 60000, undefined, 'turn-b');

    const transitioned = await store.transitionToWaitingIfActiveRequestId('key1', 'turn-a', 60000);
    expect(transitioned).toBe(false);

    expect(await store.getStatus('key1')).toBe('running');
    expect(await store.getActiveRequestId('key1')).toBe('turn-b');
  });

  it('full race: A acquires, expires, B seizes, A release no-ops, B releases normally', async () => {
    await store.tryAcquire('key1', 1000, undefined, 'turn-a');

    jest.setSystemTime(new Date('2026-07-20T00:00:01.001Z'));
    await jest.runOnlyPendingTimersAsync();

    expect(await store.tryAcquire('key1', 60000, undefined, 'turn-b')).toBe(true);

    expect(await store.releaseIfActiveRequestId('key1', 'turn-a')).toEqual({ released: false });

    const result = await store.releaseIfActiveRequestId('key1', 'turn-b');
    expect(result).toEqual({ released: true, bufferedMessage: undefined });
    expect(await store.getStatus('key1')).toBe('idle');
  });
});

describe('PostgresConversationGateStore expiry ownership', () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  it('does not notify or delete through an old timer when its generation no longer matches', async () => {
    jest.useFakeTimers();
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const query = jest.fn()
      .mockResolvedValueOnce({ rowCount: 1, rows: [{ gate_key: 'key1' }] })
      .mockResolvedValueOnce({ rowCount: 0, rows: [] });
    (store as any).pool = { query };
    const onExpiry = jest.fn();
    store.setOnExpiry(onExpiry);

    await expect(store.tryAcquire('key1', 1, 123, 'request-old')).resolves.toBe(true);
    await jest.advanceTimersByTimeAsync(30);

    expect(query).toHaveBeenLastCalledWith(
      expect.stringContaining('active_request_id IS NOT DISTINCT FROM $2'),
      ['key1', 'request-old'],
    );
    expect(onExpiry).not.toHaveBeenCalled();
  });

  it('notifies with the claimed generation only after the matching expired row is deleted', async () => {
    jest.useFakeTimers();
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const query = jest.fn()
      .mockResolvedValueOnce({ rowCount: 1, rows: [{ gate_key: 'key1' }] })
      .mockResolvedValueOnce({ rowCount: 1, rows: [{ gate_key: 'key1' }] });
    (store as any).pool = { query };
    const onExpiry = jest.fn();
    store.setOnExpiry(onExpiry);

    await store.tryAcquire('key1', 1, 123, 'request-current');
    await jest.advanceTimersByTimeAsync(30);

    expect(onExpiry).toHaveBeenCalledWith('key1', 123, 'request-current');
  });

  it('preserves an expired row request id in the idle snapshot for cancellation cleanup', async () => {
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const query = jest.fn()
      .mockResolvedValueOnce({
        rowCount: 1,
        rows: [{
          status: 'waiting_for_clarification',
          active_request_id: 'request-expired',
          expires_at: new Date(Date.now() - 1000),
          expired: true,
        }],
      })
      .mockResolvedValueOnce({ rowCount: 1, rows: [{ gate_key: 'key1' }] })
      .mockResolvedValueOnce({ rowCount: 0, rows: [] });
    (store as any).pool = { query };
    const onExpiry = jest.fn();
    store.setOnExpiry(onExpiry);

    await expect(store.getSnapshot('key1')).resolves.toEqual({
      status: 'idle',
      requestId: 'request-expired',
    });
    await expect(store.getSnapshot('key1')).resolves.toEqual({ status: 'idle' });

    expect(query.mock.calls[1]).toEqual([
      expect.stringContaining('active_request_id IS NOT DISTINCT FROM $2'),
      ['key1', 'request-expired'],
    ]);
    expect(onExpiry).toHaveBeenCalledTimes(1);
    expect(onExpiry).toHaveBeenCalledWith('key1', undefined, 'request-expired');
  });

  it('re-reads instead of reporting stale idle when an expired generation is replaced', async () => {
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const query = jest.fn()
      .mockResolvedValueOnce({
        rowCount: 1,
        rows: [{
          status: 'waiting_for_clarification',
          active_request_id: 'request-old',
          expires_at: new Date(Date.now() - 1000),
          expired: true,
        }],
      })
      .mockResolvedValueOnce({ rowCount: 0, rows: [] })
      .mockResolvedValueOnce({
        rowCount: 1,
        rows: [{
          status: 'running',
          active_request_id: 'request-new',
          expires_at: new Date(Date.now() + 60000),
          expired: false,
        }],
      });
    (store as any).pool = { query };
    const onExpiry = jest.fn();
    store.setOnExpiry(onExpiry);

    await expect(store.getSnapshot('key1')).resolves.toEqual({
      status: 'running',
      requestId: 'request-new',
    });
    expect(onExpiry).not.toHaveBeenCalled();
  });

  it('tryAcquire SQL includes expiry fallback for self-healing after restart', async () => {
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const query = jest.fn().mockResolvedValue({ rowCount: 1, rows: [{ gate_key: 'key1' }] });
    (store as any).pool = { query };

    await expect(store.tryAcquire('key1', 60000, 123, 'request-new')).resolves.toBe(true);

    const sql: string = query.mock.calls[0][0];
    expect(sql).toContain("status = 'idle'");
    expect(sql).toContain('expires_at <= NOW()');
  });

  it('acquires over an expired running row (simulated via rowCount=1)', async () => {
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const query = jest.fn().mockResolvedValue({ rowCount: 1, rows: [{ gate_key: 'key1' }] });
    (store as any).pool = { query };

    const acquired = await store.tryAcquire('key1', 60000, 456, 'request-after-restart');
    expect(acquired).toBe(true);
  });

  it('blocks when DB returns no row (active non-expired gate)', async () => {
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const query = jest.fn().mockResolvedValue({ rowCount: 0, rows: [] });
    (store as any).pool = { query };

    const acquired = await store.tryAcquire('key1', 60000, 456, 'request-blocked');
    expect(acquired).toBe(false);
  });
});

describe('PostgresConversationGateStore pool config', () => {
  it('creates pool with bounded size and connection timeout', () => {
    const store = new PostgresConversationGateStore('postgres://example.invalid/jarvis');
    const pool = (store as any).pool;
    expect(pool.options.max).toBe(5);
    expect(pool.options.connectionTimeoutMillis).toBe(5_000);
    expect(pool.options.idleTimeoutMillis).toBe(30_000);
  });
});
