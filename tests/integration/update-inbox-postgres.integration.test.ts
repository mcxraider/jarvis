import { Pool } from 'pg';
import { PostgresUpdateInbox } from '../../src/services/telegram/update-inbox.store';
import { PostgresForwardBufferStore } from '../../src/services/telegram/postgres-forward-buffer.store';

const dsn = process.env.JARVIS_INTAKE_TEST_POSTGRES_DSN;
const suite = dsn ? describe : describe.skip;
suite('durable intake against isolated Postgres', () => {
  let pool: Pool;
  beforeAll(async () => {
    pool = new Pool({ connectionString: dsn, max: 6 });
    const result = await pool.query('SELECT current_database() AS name');
    if (result.rows[0].name !== 'jarvis_intake_test') throw new Error('Use only the isolated jarvis_intake_test database');
  });
  beforeEach(async () => { await pool.query('TRUNCATE private.telegram_update_inbox, private.telegram_forward_buffers'); });
  afterAll(async () => { await pool?.end(); });
  const update = (id: number, user = 1, text = 'hello') => ({ update_id: id, message: { message_id: id, from: { id: user }, chat: { id: user }, text } });

  it('deduplicates at capacity and atomically rejects overflow', async () => {
    const inbox = new PostgresUpdateInbox(pool, 'test', 2);
    await inbox.enqueue(update(1));
    const results = await Promise.all([inbox.enqueue(update(2)), inbox.enqueue(update(3))]);
    expect(results.filter(Boolean)).toHaveLength(1);
    expect(await inbox.enqueue(update(1))).toBe(true);
    const count = await pool.query('SELECT count(*)::int AS n FROM private.telegram_update_inbox');
    expect(count.rows[0].n).toBe(2);
  });

  it('serializes conversations while keeping cancel available', async () => {
    const inbox = new PostgresUpdateInbox(pool, 'test');
    for (const item of [update(1), update(2), update(3, 2), update(4, 1, '/cancel')]) await inbox.enqueue(item);
    const first = (await inbox.claim(false))!;
    expect(first.ids).toEqual(['1']);
    expect((await inbox.claim(false))!.ids).toEqual(['3']);
    expect(await inbox.claim(false)).toBeUndefined();
    expect((await inbox.claim(true))!.ids).toEqual(['4']);
    await inbox.settle(first, 'completed');
    expect((await inbox.claim(false))!.ids).toEqual(['2']);
  });

  it('recovers expired dispatch intent and fences the previous owner', async () => {
    const inbox = new PostgresUpdateInbox(pool, 'test'); await inbox.enqueue(update(1));
    const first = (await inbox.claim(false))!;
    await inbox.dispatched(first, { route: 'invoke', userId: 'telegram:1', requestId: 'tg_update_1' });
    await pool.query("UPDATE private.telegram_update_inbox SET lease_expires_at=now()-interval '1 second'");
    const second = (await inbox.claim(false))!;
    expect(second.stage).toBe('dispatched'); expect(second.dispatch?.requestId).toBe('tg_update_1');
    expect(await inbox.renew(first)).toBe(false);
    await expect(inbox.settle(first, 'completed')).rejects.toThrow('lease lost');
    await inbox.settle(second, 'reconciliation');
  });

  it('claims an album as one durable group and keeps late members behind it', async () => {
    const inbox = new PostgresUpdateInbox(pool, 'test');
    for (const id of [1, 2]) {
      const item: any = update(id); item.message.media_group_id = 'album'; item.message.photo = [{ file_id: `photo${id}`, width: 10, height: 10 }];
      await inbox.enqueue(item);
    }
    expect(await inbox.claim(false)).toBeUndefined();
    await pool.query("UPDATE private.telegram_update_inbox SET available_at=now()-interval '1 second'");
    const group = (await inbox.claim(false))!; expect(group.ids).toEqual(['1', '2']);
    const late: any = update(3); late.message.media_group_id = 'album'; late.message.photo = [];
    await inbox.enqueue(late); await pool.query("UPDATE private.telegram_update_inbox SET available_at=now()-interval '1 second'");
    expect(await inbox.claim(false)).toBeUndefined();
    await inbox.settle(group, 'completed'); expect((await inbox.claim(false))!.ids).toEqual(['3']);
  });

  it('skips unavailable audio/image slots without blocking text in another conversation', async () => {
    const inbox = new PostgresUpdateInbox(pool, 'test');
    const audio: any = update(1); audio.message.voice = { file_id: 'voice' };
    const image: any = update(2, 2); image.message.photo = [];
    await inbox.enqueue(audio); await inbox.enqueue(image); await inbox.enqueue(update(3, 3));
    expect((await inbox.claim(false, false, false))!.ids).toEqual(['3']);
    expect((await inbox.claim(false, false, true))!.ids).toEqual(['2']);
  });

  it('preserves forward snapshots across store instances and acknowledges only the prefix', async () => {
    const one = new PostgresForwardBufferStore(pool, 'test');
    const two = new PostgresForwardBufferStore(pool, 'test');
    const msg = (id: string) => ({ id, senderName: 'Alice', text: id, receivedAt: new Date(), forwardedAt: new Date() });
    await one.push('conversation', msg('1'));
    const prefix = await two.peek('conversation');
    await one.push('conversation', msg('2')); await one.push('conversation', msg('2'));
    expect(await two.acknowledge('conversation', prefix)).toEqual({ acknowledged: true, remainingCount: 1, confirmationMessageId: undefined });
    expect((await one.peek('conversation')).map(message => message.id)).toEqual(['2']);
  });

  it('atomically admits one audio and preserves metadata across restarts and duplicate delivery', async () => {
    const first = new PostgresForwardBufferStore(pool, 'test');
    const second = new PostgresForwardBufferStore(pool, 'test');
    const audio = (id: string) => ({ id, senderName: 'Alice', text: '[audio] caption',
      receivedAt: new Date(), forwardedAt: new Date(),
      audio: { kind: 'audio' as const, fileId: id, fileName: 'memo.mp3', mimeType: 'audio/mpeg', fileSize: 1024, duration: 10 } });
    const results = await Promise.all([first.push('conversation', audio('1')), second.push('conversation', audio('2'))]);
    expect(results.filter(result => result.ok)).toHaveLength(1);
    expect(results).toContainEqual({ ok: false, reason: 'audio_already_buffered' });
    const snapshot = await second.peek('conversation');
    expect(snapshot[0].audio).toEqual(audio(snapshot[0].id!).audio);
    expect(snapshot[0].forwardedAt).toBeInstanceOf(Date);
    expect(await first.push('conversation', audio(snapshot[0].id!))).toEqual({ ok: true, count: 1 });
    await first.push('conversation', { id: 'later', senderName: 'Bob', text: 'later', receivedAt: new Date(), forwardedAt: new Date() });
    expect(await second.acknowledge('conversation', snapshot)).toMatchObject({ acknowledged: true, remainingCount: 1 });
    expect((await first.peek('conversation')).map(message => message.id)).toEqual(['later']);
    expect(await second.push('conversation', audio('next'))).toEqual({ ok: true, count: 2 });
  });

  it('denies access to public API roles and allows the runtime role', async () => {
    const connection = await pool.connect();
    try {
      await connection.query('SET ROLE anon');
      await expect(connection.query('SELECT * FROM private.telegram_update_inbox')).rejects.toThrow();
      await connection.query('RESET ROLE'); await connection.query('SET ROLE jarvis_runtime');
      expect((await connection.query('SELECT * FROM private.telegram_update_inbox')).rows).toEqual([]);
    } finally { await connection.query('RESET ROLE'); connection.release(); }
  });
});
