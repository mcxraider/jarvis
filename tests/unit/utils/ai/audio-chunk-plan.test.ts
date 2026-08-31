import { planAudioChunks } from '../../../../src/utils/ai/audio-chunk-plan';
import type { AudioChunkPlan } from '../../../../src/utils/ai/audio-chunk-plan';

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
    const chunks = planAudioChunks(30);

    expect(chunks).toEqual([
      {
        index: 0,
        startSeconds: 0,
        endSeconds: 30,
        coreStartSeconds: 0,
        coreEndSeconds: 30,
      },
    ]);
  });

  it('splits one millisecond over the core duration into two viable uploads', () => {
    const duration = 30.001;
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

  it('produces the documented geometry for 35 seconds', () => {
    expect(planAudioChunks(35)).toEqual([
      { index: 0, startSeconds: 0, endSeconds: 20, coreStartSeconds: 0, coreEndSeconds: 17.5 },
      { index: 1, startSeconds: 15, endSeconds: 35, coreStartSeconds: 17.5, coreEndSeconds: 35 },
    ]);
  });

  it('produces the documented geometry for 60 seconds', () => {
    expect(planAudioChunks(60)).toEqual([
      { index: 0, startSeconds: 0, endSeconds: 32.5, coreStartSeconds: 0, coreEndSeconds: 30 },
      { index: 1, startSeconds: 27.5, endSeconds: 60, coreStartSeconds: 30, coreEndSeconds: 60 },
    ]);
  });

  it('produces 40 equal cores with exactly five seconds of internal overlap at 20 minutes', () => {
    const duration = 1_200;
    const chunks = planAudioChunks(duration);

    expect(chunks).toHaveLength(40);
    expectAscendingIndices(chunks);
    expectContiguousCores(chunks, duration);
    expectClamped(chunks, duration);

    for (const chunk of chunks) {
      expect(chunk.coreEndSeconds - chunk.coreStartSeconds).toBe(30);
    }
    expect(chunks[0].startSeconds).toBe(0);
    expect(chunks[39].endSeconds).toBe(1_200);

    for (let i = 0; i < chunks.length - 1; i += 1) {
      expect(chunks[i].endSeconds - chunks[i + 1].startSeconds).toBe(5);
    }
  });

  it.each([1_199.9, 600.5])('keeps cores contiguous for fractional duration %p', (duration) => {
    const chunks = planAudioChunks(duration);

    expect(chunks).toHaveLength(Math.ceil(duration / 30));
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
    durations.push(30.5, 31.25, 44.75, 89.999, 300.125, 1_199.75);

    for (const duration of durations) {
      const chunks = planAudioChunks(duration);

      expect(chunks).toHaveLength(Math.ceil(duration / 30));
      expect(chunks.length).toBeLessThanOrEqual(40);
      expectAscendingIndices(chunks);
      expectContiguousCores(chunks, duration);
      expectClamped(chunks, duration);

      const coreTotal = chunks.reduce(
        (sum, chunk) => sum + (chunk.coreEndSeconds - chunk.coreStartSeconds),
        0,
      );
      expect(coreTotal).toBeCloseTo(duration, 2);

      if (duration > 30) {
        for (const chunk of chunks) {
          // 15s is the floor for balanced cores; allow the documented millisecond rounding.
          expect(chunk.coreEndSeconds - chunk.coreStartSeconds).toBeGreaterThanOrEqual(14.999);
          expect(chunk.endSeconds - chunk.startSeconds).toBeGreaterThanOrEqual(10);
        }
      }
    }
  });
});
