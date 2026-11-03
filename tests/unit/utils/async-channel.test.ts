import { AsyncChunkQueue } from '../../../src/utils/async-channel';

describe('bounded audio channel', () => {
  it('blocks extraction until a consumer makes room', async () => {
    const channel = new AsyncChunkQueue<number>(2);
    await channel.push(1); await channel.push(2);
    let pushed = false; const pending = channel.push(3).then(() => { pushed = true; });
    await Promise.resolve(); expect(pushed).toBe(false);
    expect(await channel.pull()).toBe(1); await pending;
    expect(await channel.pull()).toBe(2); expect(await channel.pull()).toBe(3);
    channel.close(); expect(await channel.pull()).toBeNull();
  });
  it('wakes a blocked producer when transcription fails', async () => {
    const channel = new AsyncChunkQueue<number>(1); await channel.push(1);
    const pending = channel.push(2); channel.close();
    await expect(pending).rejects.toThrow('Channel closed');
  });
});
