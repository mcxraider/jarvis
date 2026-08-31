// src/utils/ai/audio-chunk-plan.ts — Pure chunk geometry for long-form transcription.
//
// Whisper large-v3 is optimised for 30-second segments, so audio longer than one core
// duration is split into `ceil(D / core)` equal, non-overlapping *core* regions. Each
// upload is that core widened by half the overlap at every internal boundary, clamped
// to the file. Cores decide ownership at merge time; the overlap only exists so the
// model has context either side of a boundary.
//
//   35s → cores 0–17.5, 17.5–35   uploads 0–20,   15–35
//   60s → cores 0–30,   30–60     uploads 0–32.5, 27.5–60
//
// Equal cores (rather than fixed 30s cores plus a remainder) avoid a tiny final chunk;
// for any accepted duration the core stays above the model's 10-second minimum.

import { AUDIO_LIMITS } from './audio-limits';

export interface AudioChunkPlan {
  /** Position in the timeline, 0-based. Merge order is by this index, not completion order. */
  index: number;
  /** Upload range start on the original timeline, seconds. */
  startSeconds: number;
  /** Upload range end on the original timeline, seconds. */
  endSeconds: number;
  /** Core (ownership) range start, seconds. */
  coreStartSeconds: number;
  /** Core (ownership) range end, seconds. */
  coreEndSeconds: number;
}

export interface AudioChunkPlanOptions {
  coreSeconds?: number;
  overlapSeconds?: number;
}

// Millisecond precision keeps FFmpeg seek arguments tidy and the geometry deterministic
// under floating-point division.
const PRECISION = 1_000;

function round(value: number): number {
  return Math.round(value * PRECISION) / PRECISION;
}

export function planAudioChunks(
  durationSeconds: number,
  options: AudioChunkPlanOptions = {},
): AudioChunkPlan[] {
  const coreSeconds = options.coreSeconds ?? AUDIO_LIMITS.CORE_SECONDS;
  const overlapSeconds = options.overlapSeconds ?? AUDIO_LIMITS.OVERLAP_SECONDS;

  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    throw new Error('planAudioChunks requires a positive finite duration');
  }
  if (!Number.isFinite(coreSeconds) || coreSeconds <= 0) {
    throw new Error('planAudioChunks requires a positive finite core duration');
  }
  if (!Number.isFinite(overlapSeconds) || overlapSeconds < 0) {
    throw new Error('planAudioChunks requires a non-negative finite overlap');
  }

  const duration = round(durationSeconds);

  // Single request: no boundaries, so no overlap and no merge.
  if (duration <= coreSeconds) {
    return [
      {
        index: 0,
        startSeconds: 0,
        endSeconds: duration,
        coreStartSeconds: 0,
        coreEndSeconds: duration,
      },
    ];
  }

  const count = Math.ceil(duration / coreSeconds);
  const coreWidth = duration / count;
  const halfOverlap = overlapSeconds / 2;

  const chunks: AudioChunkPlan[] = [];
  let coreStart = 0;
  for (let index = 0; index < count; index += 1) {
    const isFirst = index === 0;
    const isLast = index === count - 1;
    // Anchor the final core to the exact duration so rounding cannot drop a tail.
    const coreEnd = isLast ? duration : round((index + 1) * coreWidth);

    chunks.push({
      index,
      startSeconds: isFirst ? 0 : round(Math.max(0, coreStart - halfOverlap)),
      endSeconds: isLast ? duration : round(Math.min(duration, coreEnd + halfOverlap)),
      coreStartSeconds: coreStart,
      coreEndSeconds: coreEnd,
    });

    coreStart = coreEnd;
  }

  return chunks;
}
