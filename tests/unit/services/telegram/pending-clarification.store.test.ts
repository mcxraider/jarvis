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

  it('clearAllForUser drops every record for one user, leaving other users untouched', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ pendingKey: 'chat-a', telegramUserId: 123 }));
    await store.save(makeRecord({ pendingKey: 'chat-b', telegramUserId: 123 }));
    await store.save(makeRecord({ pendingKey: 'other', telegramUserId: 456 }));

    await store.clearAllForUser(123, 'failed');

    expect(await store.get('chat-a')).toBeUndefined();
    expect(await store.get('chat-b')).toBeUndefined();
    expect(await store.get('other')).toBeDefined();
  });

  it('clearAllForUser is a no-op when the user has no pending records', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ telegramUserId: 456 }));
    await expect(store.clearAllForUser(123, 'failed')).resolves.toBeUndefined();
    expect(await store.get('telegram:abc123')).toBeDefined();
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

  it('round-trips and attaches a clarificationMessageId', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ clarificationMessageId: 808 }));
    expect((await store.get('telegram:abc123'))?.clarificationMessageId).toBe(808);

    await store.attachClarificationMessageId('telegram:abc123', 909);
    expect((await store.get('telegram:abc123'))?.clarificationMessageId).toBe(909);
  });

  it('attachClarificationMessageId is a no-op for a missing record', async () => {
    const store = new MemoryPendingClarificationStore();
    await expect(
      store.attachClarificationMessageId('telegram:missing', 909),
    ).resolves.toBeUndefined();
  });

  it('does not retain stale presentation ids when a record is replaced', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({
      clarificationMessageId: 11,
    }));
    await store.save(makeRecord({ threadId: 'thread-new' }));

    const retrieved = await store.get('telegram:abc123');
    expect(retrieved?.clarificationMessageId).toBeUndefined();
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
