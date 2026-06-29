import { MemoryPendingClarificationStore, PendingClarificationRecord } from '../../../../src/services/telegram/pending-clarification.store';

function makeRecord(overrides: Partial<PendingClarificationRecord> = {}): PendingClarificationRecord {
  const now = Date.now();
  return {
    pendingKey: 'telegram:abc123',
    threadId: 'thread-1',
    question: 'Confirm deletion?',
    userId: 'user-1',
    status: 'pending',
    createdAt: now,
    updatedAt: now,
    expiresAt: now + 30 * 60 * 1000,
    ...overrides,
  };
}

describe('MemoryPendingClarificationStore', () => {
  it('round-trips a record with interruptType confirm', async () => {
    const store = new MemoryPendingClarificationStore();
    const record = makeRecord({ interruptType: 'confirm' });
    await store.save(record);
    const retrieved = await store.get(record.pendingKey);
    expect(retrieved?.interruptType).toBe('confirm');
  });

  it('round-trips a record with interruptType clarify', async () => {
    const store = new MemoryPendingClarificationStore();
    const record = makeRecord({ interruptType: 'clarify' });
    await store.save(record);
    const retrieved = await store.get(record.pendingKey);
    expect(retrieved?.interruptType).toBe('clarify');
  });

  it('returns undefined for an expired record', async () => {
    const store = new MemoryPendingClarificationStore();
    const record = makeRecord({ expiresAt: Date.now() - 1 });
    await store.save(record);
    const retrieved = await store.get(record.pendingKey);
    expect(retrieved).toBeUndefined();
  });

  it('clears a record', async () => {
    const store = new MemoryPendingClarificationStore();
    const record = makeRecord();
    await store.save(record);
    await store.clear(record.pendingKey, 'completed');
    const retrieved = await store.get(record.pendingKey);
    expect(retrieved).toBeUndefined();
  });

  it('overwrites an existing record on save', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ threadId: 'thread-old', interruptType: 'clarify' }));
    await store.save(makeRecord({ threadId: 'thread-new', interruptType: 'confirm' }));
    const retrieved = await store.get('telegram:abc123');
    expect(retrieved?.threadId).toBe('thread-new');
    expect(retrieved?.interruptType).toBe('confirm');
  });

  it('clears a record as superseded (/new abandon)', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord());
    await store.clear('telegram:abc123', 'superseded');
    expect(await store.get('telegram:abc123')).toBeUndefined();
  });

  it('sweepExpired prunes only expired records', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ pendingKey: 'live', expiresAt: Date.now() + 60000 }));
    await store.save(makeRecord({ pendingKey: 'stale', expiresAt: Date.now() - 1 }));

    await store.sweepExpired();

    expect(await store.get('live')).toBeDefined();
    expect(await store.get('stale')).toBeUndefined();
  });
});
