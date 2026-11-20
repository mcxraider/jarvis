import { UpdateInboxWorker } from '../../../../src/services/telegram/update-inbox.worker';
import { InboxJob } from '../../../../src/services/telegram/update-inbox.store';
import { telegramWorkContext } from '../../../../src/services/telegram/work-context';

const job = (patch: Partial<InboxJob> = {}): InboxJob => ({ ids: ['42'], payloads: [{ update_id: 42 }], owner: 'owner', stage: 'received', attempts: 1, createdAt: new Date(), ...patch });
const store = () => ({ claim: jest.fn().mockResolvedValue(undefined), cleanup: jest.fn().mockResolvedValue(undefined), renew: jest.fn().mockResolvedValue(true), dispatched: jest.fn().mockResolvedValue(undefined), settle: jest.fn().mockResolvedValue(undefined) });
const flush = () => new Promise(resolve => setImmediate(resolve));

describe('durable inbox worker', () => {
  it('reconciles dispatched work without handling it a second time', async () => {
    const db = store(); const item = job({ stage: 'dispatched' });
    db.claim.mockImplementation(async control => control ? undefined : item);
    const processor = { handle: jest.fn(), recover: jest.fn().mockResolvedValue('reconciliation') };
    const worker = new UpdateInboxWorker(db as any, processor);
    db.claim.mockResolvedValueOnce(undefined).mockResolvedValueOnce(item).mockResolvedValue(undefined);
    worker.start(); await flush(); await worker.stop();
    expect(processor.handle).not.toHaveBeenCalled();
    expect(processor.recover).toHaveBeenCalledWith(item);
    expect(db.settle).toHaveBeenCalledWith(item, 'reconciliation');
  });

  it('persists dispatch intent before allowing a provider call', async () => {
    const db = store(); const item = job();
    db.claim.mockResolvedValueOnce(undefined).mockResolvedValueOnce(item).mockResolvedValue(undefined);
    const dispatch = { route: 'invoke' as const, userId: 'telegram:1', requestId: 'tg_update_42' };
    const processor = { handle: jest.fn(async () => { await telegramWorkContext.getStore()!.beforeDispatch(dispatch); }), recover: jest.fn() };
    const worker = new UpdateInboxWorker(db as any, processor);
    worker.start(); await flush(); await worker.stop();
    expect(db.dispatched).toHaveBeenCalledWith(item, dispatch);
    expect(db.settle).toHaveBeenCalledWith(item, 'completed');
  });

  it('waits for accepted jobs during shutdown and stops new claims', async () => {
    const db = store(); let finish!: () => void;
    db.claim.mockResolvedValueOnce(undefined).mockResolvedValueOnce(job()).mockResolvedValue(undefined);
    const worker = new UpdateInboxWorker(db as any, { handle: () => new Promise(resolve => { finish = resolve; }), recover: jest.fn() });
    worker.start(); await flush();
    let stopped = false; const stop = worker.stop().then(() => { stopped = true; });
    await flush(); expect(stopped).toBe(false);
    finish(); await stop; expect(stopped).toBe(true); expect(worker.healthy).toBe(false);
  });

  it('does not dispatch expired work', async () => {
    const db = store(); const item = job({ createdAt: new Date(Date.now() - 660_000) });
    db.claim.mockResolvedValueOnce(undefined).mockResolvedValueOnce(item).mockResolvedValue(undefined);
    const processor = { handle: jest.fn(), recover: jest.fn() };
    const worker = new UpdateInboxWorker(db as any, processor);
    worker.start(); await flush(); await worker.stop();
    expect(processor.handle).not.toHaveBeenCalled(); expect(db.settle).toHaveBeenCalledWith(item, 'failed');
  });
});

it('keeps the lease until watchdog-detached middleware actually settles', async () => {
  const db = store(); let finish!: () => void;
  db.claim.mockResolvedValueOnce(undefined).mockResolvedValueOnce(job()).mockResolvedValue(undefined);
  const processor = { handle: jest.fn(async () => {
    const handler = new Promise<void>(resolve => { finish = resolve; });
    telegramWorkContext.getStore()!.trackHandler!(handler);
    // Simulate Telegraf's outer promise resolving after its watchdog catches a timeout.
  }), recover: jest.fn() };
  const worker = new UpdateInboxWorker(db as any, processor);
  worker.start(); await flush(); expect(db.settle).not.toHaveBeenCalled();
  finish(); await worker.stop(); expect(db.settle).toHaveBeenCalledWith(expect.anything(), 'completed');
});

it('schedules status reconciliation after ambiguous transport delivery', async () => {
  const db = store(); const item = job({ stage: 'dispatched' });
  // This turn starts as received and records dispatch during processing.
  item.stage = 'received';
  db.claim.mockResolvedValueOnce(undefined).mockResolvedValueOnce(item).mockResolvedValue(undefined);
  const processor = { handle: jest.fn(async () => {
    item.stage = 'dispatched';
    telegramWorkContext.getStore()!.markAmbiguous!();
  }), recover: jest.fn() };
  const worker = new UpdateInboxWorker(db as any, processor);
  worker.start(); await flush(); await worker.stop();
  expect(db.settle).toHaveBeenCalledWith(item, 'pending');
  expect(processor.recover).not.toHaveBeenCalled();
});

describe('adaptive inbox polling', () => {
  afterEach(() => {
    jest.useRealTimers();
    delete process.env.JARVIS_INBOX_IDLE_POLL_MAX_MS;
  });
  const pollsAfter = async (db: ReturnType<typeof store>, ms: number) => {
    const before = db.claim.mock.calls.length;
    await jest.advanceTimersByTimeAsync(ms);
    return db.claim.mock.calls.length - before;
  };

  it('backs off exponentially while idle and caps the interval', async () => {
    jest.useFakeTimers();
    const db = store();
    const worker = new UpdateInboxWorker(db as any, { handle: jest.fn(), recover: jest.fn() });
    worker.start();
    await jest.advanceTimersByTimeAsync(60_000);
    // Each idle poll makes two claim calls (control + normal); the legacy fixed 250 ms
    // interval would have made ~480 in this window.
    const idle = await pollsAfter(db, 60_000);
    // ~12 polls/minute at the 5 s ceiling (±1 for timer phase).
    expect(idle).toBeGreaterThanOrEqual(22);
    expect(idle).toBeLessThanOrEqual(26);
    expect(worker.healthy).toBe(true);
    await worker.stop();
  });

  it('polls immediately on wake and resets the backoff', async () => {
    jest.useFakeTimers();
    const db = store();
    const worker = new UpdateInboxWorker(db as any, { handle: jest.fn(), recover: jest.fn() });
    worker.start();
    await jest.advanceTimersByTimeAsync(30_000);
    const item = job();
    db.claim.mockResolvedValueOnce(undefined).mockResolvedValueOnce(item);
    worker.wake();
    expect(await pollsAfter(db, 1)).toBeGreaterThanOrEqual(2);
    expect(db.settle).toHaveBeenCalledWith(item, 'completed');
    // Backoff restarted from the base interval: several polls within 1 s instead of one per 5 s.
    expect(await pollsAfter(db, 1_000)).toBeGreaterThanOrEqual(4);
    await worker.stop();
  });

  it('rejects an idle ceiling that would break the health window', () => {
    process.env.JARVIS_INBOX_IDLE_POLL_MAX_MS = '30000';
    expect(() => new UpdateInboxWorker(store() as any, { handle: jest.fn(), recover: jest.fn() })).toThrow(
      /JARVIS_INBOX_IDLE_POLL_MAX_MS/,
    );
  });
});
