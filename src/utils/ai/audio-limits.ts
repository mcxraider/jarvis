// src/utils/ai/audio-limits.ts — Shared long-audio limits and the user-facing copy
// that goes with them. Single source of truth so admission checks in the Telegram
// handlers, FileService, the streamed download, and FFmpeg preparation all agree.

export const AUDIO_LIMITS = {
  /** Hosted Telegram Bot API getFile download ceiling: exactly 20 MiB. */
  MAX_INPUT_BYTES: 20 * 1024 * 1024,
  /** Jarvis product/operational limit on decoded audio duration. */
  MAX_DURATION_SECONDS: 1_200,
  /** Whisper large-v3 is optimised for 30-second segments. */
  CORE_SECONDS: 30,
  /** Total shared audio between two neighbouring uploads (2.5s each side). */
  OVERLAP_SECONDS: 5,
  /** Groq rate limits are organization-wide, so this cap is process-global. */
  MAX_CONCURRENT_REQUESTS: 5,
  /** Attempts per chunk, including the first. */
  MAX_ATTEMPTS_PER_CHUNK: 3,
  /** Longest single Retry-After wait we will honour. */
  MAX_RETRY_AFTER_MS: 60_000,
  /** Groq's direct-attachment ceiling for one upload. */
  GROQ_MAX_ATTACHMENT_BYTES: 25 * 1024 * 1024,
} as const;

export const AUDIO_LIMIT_MESSAGES = {
  tooLarge: 'That audio file is too large. Jarvis can only accept files up to 20 MB.',
  tooLong: 'That audio is too long. Please send audio that is 20 minutes or shorter.',
} as const;
