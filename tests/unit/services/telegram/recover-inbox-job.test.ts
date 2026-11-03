import { recoverInboxJob } from '../../../../src/services/telegram/recover-inbox-job';
import { InboxJob } from '../../../../src/services/telegram/update-inbox.store';

const job = (): InboxJob => ({ ids: ['1'], owner: 'owner', attempts: 2, stage: 'dispatched', createdAt: new Date(),
  payloads: [{ update_id: 1, message: { from: { id: 1 }, chat: { id: 2 } } }],
  dispatch: { route: 'invoke', userId: 'telegram:1', requestId: 'tg_update_1' } });

describe('inbox crash recovery', () => {
  const bot = () => ({ sendMessage: jest.fn().mockResolvedValue({ message_id: 7 }), sendRichMessage: jest.fn().mockResolvedValue(undefined) });
  const gates = () => ({ getSnapshot: jest.fn().mockResolvedValue({ status: 'running', requestId: 'tg_update_1' }), releaseIfActiveRequestId: jest.fn() });
  const pending = () => ({ get: jest.fn().mockResolvedValue(undefined) });
  it('never repeats an unknown mutation', async () => {
    const client = { getRunStatus: jest.fn().mockResolvedValue({ state: 'unknown' }), invoke: jest.fn(), resume: jest.fn() };
    const telegram = bot();
    expect(await recoverInboxJob(job(), client as any, telegram as any, gates() as any, pending() as any)).toBe('reconciliation');
    expect(client.invoke).not.toHaveBeenCalled(); expect(client.resume).not.toHaveBeenCalled();
    expect(telegram.sendMessage).toHaveBeenCalled();
  });
  it('delivers a durable terminal result and settles its gate', async () => {
    const client = { getRunStatus: jest.fn().mockResolvedValue({ state: 'completed', response: { status: 'completed', thread_id: 'thread', response: 'Done' } }) };
    const telegram = bot(); const gate = gates();
    expect(await recoverInboxJob(job(), client as any, telegram as any, gate as any, pending() as any)).toBe('completed');
    expect(telegram.sendRichMessage).toHaveBeenCalledWith(2, 'Done', { requestId: 'tg_update_1' });
    expect(gate.releaseIfActiveRequestId).toHaveBeenCalled();
  });
  it('does not overwrite a newer conversation generation', async () => {
    const client = { getRunStatus: jest.fn().mockResolvedValue({ state: 'completed', response: { status: 'completed', response: 'Old' } }) };
    const telegram = bot(); const gate = gates(); gate.getSnapshot.mockResolvedValue({ status: 'running', requestId: 'new' });
    await recoverInboxJob(job(), client as any, telegram as any, gate as any, pending() as any);
    expect(telegram.sendRichMessage).not.toHaveBeenCalled(); expect(gate.releaseIfActiveRequestId).not.toHaveBeenCalled();
  });
});
