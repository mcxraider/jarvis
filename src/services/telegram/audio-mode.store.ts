export type AudioMode = 'default' | 'transcribe' | 'translate';

export interface AudioModeState {
  mode: AudioMode;
  selectedAt?: string;
  revision: number;
}

/** In-memory, one-shot mode selection scoped to the same user/chat conversation key. */
export class AudioModeStore {
  private readonly states = new Map<string, AudioModeState>();

  get(key: string): AudioModeState {
    return this.states.get(key) ?? { mode: 'default', revision: 0 };
  }

  select(key: string, mode: Exclude<AudioMode, 'default'>): AudioModeState {
    const state = {
      mode,
      selectedAt: new Date().toISOString(),
      revision: this.get(key).revision + 1,
    } as const;
    this.states.set(key, state);
    return state;
  }

  /** Reset only the selection that was processed; a newer command always wins. */
  resetIfCurrent(key: string, expected: AudioModeState): boolean {
    const current = this.get(key);
    if (current.mode !== expected.mode || current.revision !== expected.revision) return false;
    this.states.delete(key);
    return true;
  }
}
