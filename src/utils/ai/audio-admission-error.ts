// src/utils/ai/audio-admission-error.ts — Typed rejection for audio that Jarvis
// refuses on size or duration grounds. Carries its own user-facing copy so
// classifyError() does not have to pattern-match on message text.

import { AUDIO_LIMIT_MESSAGES } from './audio-limits';

export type AudioAdmissionReason = 'too_large' | 'too_long';

const USER_MESSAGES: Record<AudioAdmissionReason, string> = {
  too_large: AUDIO_LIMIT_MESSAGES.tooLarge,
  too_long: AUDIO_LIMIT_MESSAGES.tooLong,
};

export class AudioAdmissionError extends Error {
  readonly reason: AudioAdmissionReason;
  readonly userMessage: string;
  /** Observed value that tripped the limit: bytes for too_large, seconds for too_long. */
  readonly observed?: number;
  readonly limit?: number;

  constructor(reason: AudioAdmissionReason, detail?: { observed?: number; limit?: number }) {
    super(`Audio rejected: ${reason}`);
    this.name = 'AudioAdmissionError';
    this.reason = reason;
    this.userMessage = USER_MESSAGES[reason];
    this.observed = detail?.observed;
    this.limit = detail?.limit;
  }
}

export function isAudioAdmissionError(value: unknown): value is AudioAdmissionError {
  return value instanceof AudioAdmissionError;
}
