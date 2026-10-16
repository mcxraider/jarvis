import { AudioModeStore } from '../../../../src/services/telegram/audio-mode.store';

describe('AudioModeStore', () => {
  it('defaults to default and isolates conversations', () => {
    const store = new AudioModeStore();
    store.select('chat-a:user-a', 'transcribe');
    expect(store.get('chat-a:user-a').mode).toBe('transcribe');
    expect(store.get('chat-a:user-b').mode).toBe('default');
    expect(store.get('chat-b:user-a').mode).toBe('default');
  });

  it('replaces a pending mode and does not let an older request reset it', () => {
    const store = new AudioModeStore();
    const transcribe = store.select('conversation', 'transcribe');
    store.select('conversation', 'translate');
    expect(store.resetIfCurrent('conversation', transcribe)).toBe(false);
    expect(store.get('conversation').mode).toBe('translate');
  });

  it('resets the successfully processed selection to default', () => {
    const store = new AudioModeStore();
    const selected = store.select('conversation', 'transcribe');
    expect(store.resetIfCurrent('conversation', selected)).toBe(true);
    expect(store.get('conversation').mode).toBe('default');
  });
});
