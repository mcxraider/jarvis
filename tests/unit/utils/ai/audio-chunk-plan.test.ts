import { planAudioChunks } from '../../../../src/utils/ai/audio-chunk-plan';
import type { AudioChunkPlan } from '../../../../src/utils/ai/audio-chunk-plan';
import { AUDIO_LIMITS } from '../../../../src/utils/ai/audio-limits';

/** Cores must tile [0, duration] with no gap and no overlap. */
function expectContiguousCores(chunks: AudioChunkPlan[], duration: number): void {
  expect(chunks[0].coreStartSeconds).toBe(0);
  expect(chunks[chunks.length - 1].coreEndSeconds).toBeCloseTo(duration, 3);
  for (let i = 0; i < chunks.length - 1; i += 1) {
    expect(chunks[i].coreEndSeconds).toBe(chunks[i + 1].coreStartSeconds);
    expect(chunks[i].coreEndSeconds).toBeGreaterThan(chunks[i].coreStartSeconds);
  }
}

function expectClamped(chunks: AudioChunkPlan[], duration: number): void {
  for (const chunk of chunks) {
    expect(chunk.startSeconds).toBeGreaterThanOrEqual(0);
    expect(chunk.endSeconds).toBeLessThanOrEqual(duration);
    // The upload always contains its own core.
    expect(chunk.startSeconds).toBeLessThanOrEqual(chunk.coreStartSeconds);
    expect(chunk.endSeconds).toBeGreaterThanOrEqual(chunk.coreEndSeconds);
  }
}

function expectAscendingIndices(chunks: AudioChunkPlan[]): void {
  expect(chunks.map((chunk) => chunk.index)).toEqual(chunks.map((_, i) => i));
}

describe('planAudioChunks', () => {
  it('makes a single chunk with no overlap at exactly the core duration', () => {
    const chunks = planAudioChunks(AUDIO_LIMITS.CORE_SECONDS);

    expect(chunks).toEqual([
      {
        index: 0,
        startSeconds: 0,
        endSeconds: AUDIO_LIMITS.CORE_SECONDS,
        coreStartSeconds: 0,
        coreEndSeconds: AUDIO_LIMITS.CORE_SECONDS,
      },
    ]);
  });

  it('splits one millisecond over the core duration into two viable uploads', () => {
    const duration = AUDIO_LIMITS.CORE_SECONDS + 0.001;
    const chunks = planAudioChunks(duration);

    expect(chunks).toHaveLength(2);
    expectAscendingIndices(chunks);
    expectContiguousCores(chunks, duration);
    expectClamped(chunks, duration);

    // Balanced cores, up to the module's millisecond rounding.
    const coreWidths = chunks.map((chunk) => chunk.coreEndSeconds - chunk.coreStartSeconds);
    expect(coreWidths[0]).toBeCloseTo(coreWidths[1], 2);

    // Every upload must clear Whisper large-v3's documented 10-second minimum.
    for (const chunk of chunks) {
      expect(chunk.endSeconds - chunk.startSeconds).toBeGreaterThanOrEqual(10);
    }
  });

  it('keeps a 35-second recording in one production-default chunk', () => {
    expect(planAudioChunks(35)).toEqual([
      { index: 0, startSeconds: 0, endSeconds: 35, coreStartSeconds: 0, coreEndSeconds: 35 },
    ]);
  });

  it('produces zero-overlap production-default geometry for 60 seconds', () => {
    expect(planAudioChunks(60)).toEqual([
      { index: 0, startSeconds: 0, endSeconds: 30, coreStartSeconds: 0, coreEndSeconds: 30 },
      { index: 1, startSeconds: 30, endSeconds: 60, coreStartSeconds: 30, coreEndSeconds: 60 },
    ]);
  });

  it('produces 27 equal cores with no internal overlap at 20 minutes', () => {
    const duration = AUDIO_LIMITS.MAX_DURATION_SECONDS;
    const chunks = planAudioChunks(duration);

    expect(chunks).toHaveLength(27);
    expectAscendingIndices(chunks);
    expectContiguousCores(chunks, duration);
    expectClamped(chunks, duration);

    for (const chunk of chunks) {
      expect(chunk.coreEndSeconds - chunk.coreStartSeconds).toBeCloseTo(duration / 27, 2);
    }
    expect(chunks[0].startSeconds).toBe(0);
    expect(chunks[26].endSeconds).toBe(duration);

    for (let i = 0; i < chunks.length - 1; i += 1) {
      expect(chunks[i].endSeconds).toBe(chunks[i + 1].startSeconds);
    }
  });

  it.each([1_199.9, 600.5])('keeps cores contiguous for fractional duration %p', (duration) => {
    const chunks = planAudioChunks(duration);

    expect(chunks).toHaveLength(Math.ceil(duration / AUDIO_LIMITS.CORE_SECONDS));
    expectAscendingIndices(chunks);
    expectContiguousCores(chunks, duration);
    expectClamped(chunks, duration);
  });

  it('honours custom core and overlap options', () => {
    const chunks = planAudioChunks(25, { coreSeconds: 10, overlapSeconds: 4 });

    expect(chunks).toHaveLength(3);
    expectContiguousCores(chunks, 25);
    expectClamped(chunks, 25);
    expect(chunks[0].endSeconds - chunks[1].startSeconds).toBeCloseTo(4, 6);
    expect(chunks[1].endSeconds - chunks[2].startSeconds).toBeCloseTo(4, 6);
  });

  it('makes uploads identical to cores when the overlap is zero', () => {
    const chunks = planAudioChunks(60, { overlapSeconds: 0 });

    expect(chunks).toHaveLength(2);
    for (const chunk of chunks) {
      expect(chunk.startSeconds).toBe(chunk.coreStartSeconds);
      expect(chunk.endSeconds).toBe(chunk.coreEndSeconds);
    }
  });

  it.each([0, -1, NaN, Infinity, -Infinity])('rejects invalid duration %p', (duration) => {
    expect(() => planAudioChunks(duration)).toThrow(
      'planAudioChunks requires a positive finite duration',
    );
  });

  it.each([0, -5, NaN, Infinity])('rejects invalid core duration %p', (coreSeconds) => {
    expect(() => planAudioChunks(60, { coreSeconds })).toThrow(
      'planAudioChunks requires a positive finite core duration',
    );
  });

  it.each([-0.1, -5, NaN, Infinity])('rejects invalid overlap %p', (overlapSeconds) => {
    expect(() => planAudioChunks(60, { overlapSeconds })).toThrow(
      'planAudioChunks requires a non-negative finite overlap',
    );
  });

  it('holds its invariants across the whole accepted duration range', () => {
    const durations: number[] = [];
    for (let duration = 1; duration <= 1_200; duration += 7) durations.push(duration);
    durations.push(45.5, 46.25, 67.25, 89.999, 300.125, 1_199.75);

    for (const duration of durations) {
      const chunks = planAudioChunks(duration);

      expect(chunks).toHaveLength(Math.ceil(duration / AUDIO_LIMITS.CORE_SECONDS));
      expect(chunks.length).toBeLessThanOrEqual(
        Math.ceil(AUDIO_LIMITS.MAX_DURATION_SECONDS / AUDIO_LIMITS.CORE_SECONDS),
      );
      expectAscendingIndices(chunks);
      expectContiguousCores(chunks, duration);
      expectClamped(chunks, duration);

      const coreTotal = chunks.reduce(
        (sum, chunk) => sum + (chunk.coreEndSeconds - chunk.coreStartSeconds),
        0,
      );
      expect(coreTotal).toBeCloseTo(duration, 2);

      if (duration > AUDIO_LIMITS.CORE_SECONDS) {
        for (const chunk of chunks) {
          // Half a configured core is the floor for balanced cores; allow millisecond rounding.
          expect(chunk.coreEndSeconds - chunk.coreStartSeconds).toBeGreaterThanOrEqual(
            AUDIO_LIMITS.CORE_SECONDS / 2 - 0.001,
          );
          expect(chunk.endSeconds - chunk.startSeconds).toBeGreaterThanOrEqual(10);
        }
      }
    }
  });
});
