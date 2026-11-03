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
