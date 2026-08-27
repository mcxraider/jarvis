import {
  MemoryPendingClarificationStore,
  PendingClarificationRecord,
  PostgresPendingClarificationStore,
} from '../../../../src/services/telegram/pending-clarification.store';

function makeRecord(
  overrides: Partial<PendingClarificationRecord> = {},
): PendingClarificationRecord {
  const now = Date.now();
  return {
    pendingKey: 'telegram:abc123',
    threadId: 'thread-1',
    question: 'Confirm deletion?',
    userId: 'user-1',
    imageBatches: [[]],
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

  it('defensively copies nested image batches and clears them on expiry', async () => {
    const store = new MemoryPendingClarificationStore();
    const image = {
      image_url: 'data:image/jpeg;base64,/9j/2Q==' as const,
      detail: 'auto' as const,
    };
    const record = makeRecord({ imageBatches: [[image]], requestId: 'request-current' });
    await store.save(record);
    record.imageBatches![0][0].image_url = 'data:image/jpeg;base64,/9j/2g==';
    const firstRead = await store.get(record.pendingKey);
    expect(firstRead?.imageBatches?.[0][0].image_url).toBe('data:image/jpeg;base64,/9j/2Q==');
    firstRead!.imageBatches![0].length = 0;
    expect((await store.get(record.pendingKey))?.imageBatches?.[0][0].image_url).toBe(
      'data:image/jpeg;base64,/9j/2Q==',
    );
    expect(
      (await store.expireIfMatches(record.pendingKey, { requestId: 'request-current' }))
        ?.imageBatches,
    ).toEqual([]);
  });

  it('returns undefined for an expired record', async () => {
    const store = new MemoryPendingClarificationStore();
    const record = makeRecord({ expiresAt: Date.now() - 1 });
    await store.save(record);
    const retrieved = await store.get(record.pendingKey);
    expect(retrieved).toBeUndefined();
  });

  it('atomically claims an expired record for the matching request generation', async () => {
    const store = new MemoryPendingClarificationStore();
    const record = makeRecord({
      requestId: 'request-current',
      promptMessageId: 77,
      expiresAt: Date.now() - 1,
    });
    await store.save(record);

    expect(await store.get(record.pendingKey)).toBeUndefined();
    await expect(
      store.expireIfMatches(record.pendingKey, {
        requestId: 'request-stale',
      }),
    ).resolves.toBeUndefined();

    await expect(
      store.expireIfMatches(record.pendingKey, {
        requestId: 'request-current',
      }),
    ).resolves.toEqual(
      expect.objectContaining({
        requestId: 'request-current',
        promptMessageId: 77,
        imageBatches: [],
        status: 'expired',
      }),
    );
    await expect(
      store.expireIfMatches(record.pendingKey, {
        requestId: 'request-current',
      }),
    ).resolves.toBeUndefined();
  });

  it('allows a gate owner to claim an orphaned pending generation without a token', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ requestId: 'orphan-request', expiresAt: Date.now() - 1 }));

    await expect(store.expireIfMatches('telegram:abc123')).resolves.toEqual(
      expect.objectContaining({ requestId: 'orphan-request', status: 'expired' }),
    );
  });

  it('clears a record', async () => {
    const store = new MemoryPendingClarificationStore();
    const record = makeRecord();
    await store.save(record);
    await store.clear(record.pendingKey, 'completed');
    const retrieved = await store.get(record.pendingKey);
    expect(retrieved).toBeUndefined();
  });

  it('compare-clears only the exact pending request/thread snapshot', async () => {
    const store = new MemoryPendingClarificationStore();
    const oldSnapshot = makeRecord({ threadId: 'thread-old', requestId: 'request-old' });
    await store.save(oldSnapshot);
    await store.save(makeRecord({ threadId: 'thread-new', requestId: 'request-new' }));

    await expect(store.clearIfMatches(oldSnapshot.pendingKey, oldSnapshot, 'failed')).resolves.toBe(
      false,
    );
    expect((await store.get(oldSnapshot.pendingKey))?.requestId).toBe('request-new');

    await expect(
      store.clearIfMatches(
        oldSnapshot.pendingKey,
        { threadId: 'thread-new', requestId: 'request-new' },
        'completed',
      ),
    ).resolves.toBe(true);
    expect(await store.get(oldSnapshot.pendingKey)).toBeUndefined();
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

  it('does not attach a stale clarification id to a newer pending snapshot', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ threadId: 'thread-new', requestId: 'request-new' }));

    await expect(
      store.attachClarificationMessageIdIfMatches(
        'telegram:abc123',
        { threadId: 'thread-old', requestId: 'request-old' },
        909,
      ),
    ).resolves.toBe(false);
    expect((await store.get('telegram:abc123'))?.clarificationMessageId).toBeUndefined();
  });

  it('attaches a generic prompt id only to the exact pending snapshot', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(makeRecord({ threadId: 'thread-new', requestId: 'request-new' }));

    await expect(
      store.attachPromptMessageIdIfMatches(
        'telegram:abc123',
        { threadId: 'thread-old', requestId: 'request-old' },
        707,
      ),
    ).resolves.toBe(false);
    await expect(
      store.attachPromptMessageIdIfMatches(
        'telegram:abc123',
        { threadId: 'thread-new', requestId: 'request-new' },
        808,
      ),
    ).resolves.toBe(true);
    expect((await store.get('telegram:abc123'))?.promptMessageId).toBe(808);
  });

  it('does not retain stale presentation ids when a record is replaced', async () => {
    const store = new MemoryPendingClarificationStore();
    await store.save(
      makeRecord({
        clarificationMessageId: 11,
        promptMessageId: 12,
      }),
    );
    await store.save(makeRecord({ threadId: 'thread-new' }));

    const retrieved = await store.get('telegram:abc123');
    expect(retrieved?.clarificationMessageId).toBeUndefined();
    expect(retrieved?.promptMessageId).toBeUndefined();
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

describe('PostgresPendingClarificationStore expiry claim', () => {
  it('validates image batches on read and writes them through the upsert', async () => {
    const store = new PostgresPendingClarificationStore('postgres://example.invalid/jarvis');
    const now = new Date();
    const imageBatches = [
      [
        {
          image_url: 'data:image/jpeg;base64,/9j/2Q==' as const,
          detail: 'auto' as const,
        },
      ],
      [],
    ];
    const query = jest.fn().mockResolvedValue({
      rowCount: 1,
      rows: [
        {
          pending_key: 'telegram:abc123',
          thread_id: 'thread-1',
          question: 'Which?',
          telegram_user_id: 123,
          chat_id: '456',
          user_id: 'user-1',
          request_id: 'request-current',
          interrupt_type: 'clarify',
          clarification_message_id: null,
          prompt_message_id: null,
          image_batches: imageBatches,
          status: 'pending',
          created_at: now,
          updated_at: now,
          expires_at: new Date(now.getTime() + 60000),
        },
      ],
    });
    (store as any).pool = { query };

    expect((await store.get('telegram:abc123'))?.imageBatches).toEqual(imageBatches);
    await store.save(makeRecord({ imageBatches }));
    expect(query.mock.calls[1][0]).toContain('image_batches = EXCLUDED.image_batches');
    expect(query.mock.calls[1][1]).toContain(JSON.stringify(imageBatches));
  });

  it('clears durable image data on terminal update paths', async () => {
    const store = new PostgresPendingClarificationStore('postgres://example.invalid/jarvis');
    const query = jest.fn().mockResolvedValue({ rowCount: 1, rows: [] });
    (store as any).pool = { query };

    await store.clear('key', 'completed');
    await store.clearIfMatches('key', { threadId: 'thread', requestId: 'request' }, 'failed');
    await store.clearAllForUser(42, 'superseded');
    await store.sweepExpired();

    expect(query.mock.calls.every(([sql]) => sql.includes("image_batches = '[]'::jsonb"))).toBe(
      true,
    );
  });

  it('uses a null-safe generation predicate and returns expired prompt metadata', async () => {
    const store = new PostgresPendingClarificationStore('postgres://example.invalid/jarvis');
    const now = new Date();
    const query = jest.fn().mockResolvedValue({
      rowCount: 1,
      rows: [
        {
          pending_key: 'telegram:abc123',
          thread_id: 'thread-1',
          question: 'Confirm deletion?',
          telegram_user_id: 123,
          chat_id: '456',
          user_id: 'user-1',
          request_id: 'request-current',
          interrupt_type: 'confirm',
          clarification_message_id: null,
          prompt_message_id: '77',
          status: 'expired',
          created_at: now,
          updated_at: now,
          expires_at: now,
        },
      ],
    });
    (store as any).pool = { query };

    await expect(
      store.expireIfMatches('telegram:abc123', {
        requestId: 'request-current',
      }),
    ).resolves.toEqual(
      expect.objectContaining({
        requestId: 'request-current',
        promptMessageId: 77,
        status: 'expired',
      }),
    );
    expect(query).toHaveBeenCalledWith(
      expect.stringContaining('request_id IS NOT DISTINCT FROM $3'),
      ['telegram:abc123', false, 'request-current'],
    );
    expect(query.mock.calls[0][0]).toContain("image_batches = '[]'::jsonb");
  });
});
