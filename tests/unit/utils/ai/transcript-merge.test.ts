import { planAudioChunks } from '../../../../src/utils/ai/audio-chunk-plan';
import type { AudioChunkPlan } from '../../../../src/utils/ai/audio-chunk-plan';
import { mergeChunkTranscriptions } from '../../../../src/utils/ai/transcript-merge';
import type {
  ChunkMergeInput,
  TranscriptSegment,
  TranscriptWord,
} from '../../../../src/utils/ai/transcript-merge';

/**
 * Builds the words a chunk would come back with, assuming one word per second on the original
 * timeline: word `n` occupies global `[n, n + 1)`. Only words fully inside the chunk's *upload*
 * range are present, so the shared overlap audio really does appear in both chunks' word lists.
 * Timestamps are chunk-local, exactly as Groq returns them.
 */
function secondlyWords(plan: AudioChunkPlan, durationSeconds: number): TranscriptWord[] {
  const words: TranscriptWord[] = [];
  for (let n = 0; n < Math.floor(durationSeconds); n += 1) {
    if (n < plan.startSeconds || n + 1 > plan.endSeconds) continue;
    words.push({ word: `w${n}`, start: n - plan.startSeconds, end: n + 1 - plan.startSeconds });
  }
  return words;
}

function secondlyInputs(durationSeconds: number): ChunkMergeInput[] {
  return planAudioChunks(durationSeconds).map((plan) => {
    const words = secondlyWords(plan, durationSeconds);
    return {
      plan,
      transcription: { text: words.map((w) => w.word).join(' '), words },
    };
  });
}

function expectedSentence(durationSeconds: number): string {
  return Array.from({ length: Math.floor(durationSeconds) }, (_, n) => `w${n}`).join(' ');
}

/** No leading space, no trailing space, no double space — ever. */
function expectTidy(text: string): void {
  expect(text).not.toMatch(/^\s/);
  expect(text).not.toMatch(/\s$/);
  expect(text).not.toMatch(/\s\s/);
}

function textOnlyInputs(texts: string[]): ChunkMergeInput[] {
  return planAudioChunks(30 * texts.length).map((plan, i) => ({
    plan,
    transcription: { text: texts[i] },
  }));
}

/**
 * A 35s recording — cores `0–17.5` and `17.5–35` — where both chunks recognised the word sitting
 * on the boundary, but timed it differently: `firstMid` is the earlier chunk's reading of its
 * global midpoint, `secondMid` the later chunk's. Real Whisper never agrees to the millisecond,
 * so this is the normal case, not a pathological one.
 */
function straddleInputs(
  firstMid: number,
  secondMid: number,
  spoken: { first?: string; second?: string } = {},
): ChunkMergeInput[] {
  const [first, last] = planAudioChunks(35);
  const heard = (word: string, mid: number, plan: AudioChunkPlan): TranscriptWord => ({
    word,
    start: mid - 0.5 - plan.startSeconds,
    end: mid + 0.5 - plan.startSeconds,
  });

  return [
    {
      plan: first,
      transcription: {
        text: 'before straddle',
        words: [heard('before', 16.4, first), heard(spoken.first ?? 'straddle', firstMid, first)],
      },
    },
    {
      plan: last,
      transcription: {
        text: 'straddle after',
        words: [heard(spoken.second ?? 'straddle', secondMid, last), heard('after', 18.6, last)],
      },
    },
  ];
}

describe('mergeChunkTranscriptions — single chunk', () => {
  it('passes the transcript through without de-duplication', () => {
    const [plan] = planAudioChunks(20);
    const result = mergeChunkTranscriptions([
      {
        plan,
        transcription: {
          text: '  the the same words twice twice  ',
          segments: [{ id: 0, start: 0, end: 20, text: 'the the same words twice twice' }],
        },
      },
    ]);

    expect(result.strategy).toBe('single');
    expect(result.degraded).toBe(false);
    expect(result.text).toBe('the the same words twice twice');
    expect(result.segments).toEqual([
      { id: 0, start: 0, end: 20, text: 'the the same words twice twice' },
    ]);
  });

  it('rebases the only chunk onto a non-zero start offset', () => {
    const plan: AudioChunkPlan = {
      index: 3,
      startSeconds: 90,
      endSeconds: 120,
      coreStartSeconds: 92.5,
      coreEndSeconds: 117.5,
    };
    const result = mergeChunkTranscriptions([
      {
        plan,
        transcription: {
          text: 'tail of a longer recording',
          segments: [
            { id: 0, start: 1.25, end: 8.5, text: 'tail of', avg_logprob: -0.2 },
            { id: 1, start: 8.5, end: undefined, text: 'a longer recording' },
          ],
        },
      },
    ]);

    expect(result.strategy).toBe('single');
    expect(result.segments).toEqual([
      { id: 0, start: 91.25, end: 98.5, text: 'tail of', avg_logprob: -0.2 },
      { id: 1, start: 98.5, end: undefined, text: 'a longer recording' },
    ]);
  });
});

describe('mergeChunkTranscriptions — words strategy', () => {
  it('keeps every boundary word exactly once across two chunks', () => {
    const inputs = secondlyInputs(60);

    // Sanity: the overlap region 27.5–32.5 really is transcribed twice.
    expect(inputs[0].transcription.words?.map((w) => w.word)).toContain('w31');
    expect(inputs[1].transcription.words?.map((w) => w.word)).toContain('w28');

    const result = mergeChunkTranscriptions(inputs);

    expect(result.strategy).toBe('words');
    expect(result.degraded).toBe(false);
    expect(result.segments).toEqual([]);
    expect(result.text).toBe(expectedSentence(60));
    expectTidy(result.text);

    const words = result.text.split(' ');
    expect(new Set(words).size).toBe(words.length);
    for (const boundaryWord of ['w28', 'w29', 'w30', 'w31']) {
      expect(words.filter((w) => w === boundaryWord)).toHaveLength(1);
    }
  });

  it('drops nothing and duplicates nothing across three chunks', () => {
    const inputs = secondlyInputs(90);
    expect(inputs).toHaveLength(3);

    const result = mergeChunkTranscriptions(inputs);

    expect(result.strategy).toBe('words');
    expect(result.text).toBe(expectedSentence(90));
    const words = result.text.split(' ');
    expect(words).toHaveLength(90);
    expect(new Set(words).size).toBe(90);
  });

  it('merges in timeline order even when the inputs arrive out of order', () => {
    const inputs = secondlyInputs(90);
    const shuffled = [inputs[2], inputs[0], inputs[1]];

    expect(mergeChunkTranscriptions(shuffled).text).toBe(expectedSentence(90));
  });

  it('retains a word whose midpoint sits exactly on the file duration', () => {
    const [first, last] = planAudioChunks(60);
    const result = mergeChunkTranscriptions([
      {
        plan: first,
        transcription: { text: 'opening', words: [{ word: 'opening', start: 0, end: 1 }] },
      },
      {
        plan: last,
        transcription: {
          text: 'final',
          // Global 59.5 → 60.5, so the midpoint is exactly the 60s duration.
          words: [{ word: 'final', start: 32, end: 33 }],
        },
      },
    ]);

    expect(result.text).toBe('opening final');
  });

  it('never inserts a space before closing punctuation or after an opening bracket', () => {
    const [first, last] = planAudioChunks(60);
    const result = mergeChunkTranscriptions([
      {
        plan: first,
        transcription: {
          text: 'Hello , world .',
          words: [
            { word: 'Hello', start: 1, end: 2 },
            { word: ',', start: 2, end: 2.1 },
            { word: 'world', start: 2.1, end: 3 },
            { word: '.', start: 3, end: 3.1 },
          ],
        },
      },
      {
        plan: last,
        transcription: {
          text: 'Really ? ( yes )',
          words: [
            { word: 'Really', start: 12.5, end: 13.5 },
            { word: '?', start: 13.5, end: 13.6 },
            { word: '(', start: 13.6, end: 13.7 },
            { word: 'yes', start: 13.7, end: 14.5 },
            { word: ')', start: 14.5, end: 14.6 },
          ],
        },
      },
    ]);

    expect(result.strategy).toBe('words');
    expect(result.text).toBe('Hello, world. Really? (yes)');
    expectTidy(result.text);
  });

  it('emits owned segments alongside the word text', () => {
    const [first, last] = planAudioChunks(60);
    const result = mergeChunkTranscriptions([
      {
        plan: first,
        transcription: {
          text: 'one',
          words: [{ word: 'one', start: 1, end: 2 }],
          segments: [
            { id: 0, start: 1, end: 2, text: 'one', no_speech_prob: 0.01 },
            // Global 28–32, midpoint 30 — sitting on the core edge, so both chunks claim it.
            { id: 1, start: 28, end: 32, text: 'shared' },
          ],
        },
      },
      {
        plan: last,
        transcription: {
          text: 'two',
          words: [{ word: 'two', start: 12.5, end: 13.5 }],
          segments: [{ id: 0, start: 0.5, end: 4.5, text: 'shared', no_speech_prob: 0.02 }],
        },
      },
    ]);

    expect(result.text).toBe('one two');
    // The seam keeps the earlier chunk's reading of the shared segment, as the words path does.
    expect(result.segments).toEqual([
      { id: 0, start: 1, end: 2, text: 'one', no_speech_prob: 0.01 },
      { id: 1, start: 28, end: 32, text: 'shared' },
    ]);
  });

  it('contributes nothing for a chunk whose retained set is empty', () => {
    const [first, last] = planAudioChunks(60);
    const result = mergeChunkTranscriptions([
      {
        plan: first,
        transcription: { text: 'alpha', words: [{ word: 'alpha', start: 1, end: 2 }] },
      },
      {
        plan: last,
        // Every word lands back inside the first chunk's core, so this chunk retains nothing.
        transcription: {
          text: 'alpha again',
          words: [
            { word: 'alpha', start: 0, end: 0.5 },
            { word: 'again', start: 0.5, end: 1 },
          ],
        },
      },
    ]);

    expect(result.text).toBe('alpha');
    expectTidy(result.text);
  });
});

describe('mergeChunkTranscriptions — disagreeing boundary timestamps', () => {
  it('emits a boundary word once when both chunks time it inside their own core', () => {
    const result = mergeChunkTranscriptions(straddleInputs(17.49, 17.51));

    expect(result.strategy).toBe('words');
    expect(result.text).toBe('before straddle after');
    expect(result.text.split(' ').filter((w) => w === 'straddle')).toHaveLength(1);
    expectTidy(result.text);
  });

  it('emits a boundary word once when neither chunk times it inside its own core', () => {
    const result = mergeChunkTranscriptions(straddleInputs(17.51, 17.49));

    expect(result.strategy).toBe('words');
    expect(result.text).toBe('before straddle after');
    expectTidy(result.text);
  });

  it('de-duplicates a boundary word across differing casing and punctuation', () => {
    const result = mergeChunkTranscriptions(
      straddleInputs(17.49, 17.51, { first: 'Straddle,', second: 'straddle' }),
    );

    expect(result.text).toBe('before Straddle, after');
    expectTidy(result.text);
  });

  it('keeps both readings, bounded to the seam, when the two chunks heard different words', () => {
    const result = mergeChunkTranscriptions(
      straddleInputs(17.49, 17.51, { first: 'straddle', second: 'straddled' }),
    );

    // Nothing is lost and the stutter cannot spread past the boundary pair — the alternative,
    // guessing which recognition to discard, is what loses real speech.
    expect(result.text).toBe('before straddle straddled after');
    expectTidy(result.text);
  });

  it('emits a boundary segment once when the two chunks time it either side of the core', () => {
    const [first, last] = planAudioChunks(35);
    const result = mergeChunkTranscriptions([
      {
        plan: first,
        transcription: {
          text: 'Before. Straddle.',
          segments: [
            { id: 0, start: 0, end: 16, text: 'Before.' },
            // Global midpoint 17.49 — just inside this chunk's core.
            { id: 1, start: 16.99, end: 17.99, text: 'Straddle.' },
          ],
        },
      },
      {
        plan: last,
        transcription: {
          text: 'Straddle. After.',
          segments: [
            // Global midpoint 17.51 — just inside *this* chunk's core.
            { id: 0, start: 2.01, end: 3.01, text: 'Straddle.' },
            { id: 1, start: 3.5, end: 20, text: 'After.' },
          ],
        },
      },
    ]);

    expect(result.strategy).toBe('segments');
    expect(result.segments.filter((s) => s.text === 'Straddle.')).toHaveLength(1);
    expect(result.text).toBe('Before. Straddle. After.');
  });
});

describe('mergeChunkTranscriptions — segments strategy', () => {
  const quality = { avg_logprob: -0.31, no_speech_prob: 0.04, compression_ratio: 1.7 };

  function segmentInputs(): ChunkMergeInput[] {
    const [first, last] = planAudioChunks(60);
    return [
      {
        plan: first,
        transcription: {
          text: 'First part. Second part. Shared bridge.',
          words: [{ word: 'First', start: 0, end: 1 }],
          segments: [
            { id: 0, start: 0, end: 10, text: ' First part.', ...quality },
            { id: 1, start: 10, end: 20, text: ' Second part.', ...quality },
            // Global 28–32, midpoint 30 — on the core edge, so both chunks claim it.
            { id: 2, start: 28, end: 32, text: ' Shared bridge.', ...quality },
          ],
        },
      },
      {
        plan: last,
        transcription: {
          text: 'Shared bridge. Third part.',
          // No words at all → the whole merge must fall back to segments.
          segments: [
            { id: 0, start: 0.5, end: 4.5, text: ' Shared bridge.', ...quality },
            { id: 1, start: 4.5, end: 32.5, text: ' Third part.', ...quality },
          ],
        },
      },
    ];
  }

  it('falls back to segments when any chunk lacks words', () => {
    const result = mergeChunkTranscriptions(segmentInputs());

    expect(result.strategy).toBe('segments');
    expect(result.degraded).toBe(false);
    expect(result.text).toBe('First part. Second part. Shared bridge. Third part.');
    expectTidy(result.text);
  });

  it('keeps the boundary segment exactly once with global timestamps and quality metadata', () => {
    const result = mergeChunkTranscriptions(segmentInputs());

    expect(result.segments.filter((s) => s.text?.includes('Shared bridge'))).toHaveLength(1);
    expect(result.segments).toEqual([
      { id: 0, start: 0, end: 10, text: ' First part.', ...quality },
      { id: 1, start: 10, end: 20, text: ' Second part.', ...quality },
      // Both chunks claim the bridge; the earlier chunk's copy is the one kept.
      { id: 2, start: 28, end: 32, text: ' Shared bridge.', ...quality },
      { id: 1, start: 32, end: 60, text: ' Third part.', ...quality },
    ]);
  });
});

describe('mergeChunkTranscriptions — a chunk that carries nothing', () => {
  it('keeps the words strategy when a middle chunk was genuinely silent', () => {
    const inputs = secondlyInputs(90);
    inputs[1] = { plan: inputs[1].plan, transcription: { text: '', words: [], segments: [] } };

    const result = mergeChunkTranscriptions(inputs);

    expect(result.strategy).toBe('words');
    expect(result.degraded).toBe(false);

    const words = result.text.split(' ');
    expect(new Set(words).size).toBe(words.length);
    expect(words[0]).toBe('w0');
    expect(words[words.length - 1]).toBe('w89');
    expectTidy(result.text);
  });

  it('still demotes when a chunk has text but no timestamps to place it with', () => {
    const inputs = secondlyInputs(90);
    inputs[1] = { plan: inputs[1].plan, transcription: { text: 'unplaceable speech' } };

    const result = mergeChunkTranscriptions(inputs);

    expect(result.strategy).toBe('text');
    expect(result.degraded).toBe(true);
    expect(result.text).toContain('unplaceable speech');
  });
});

describe('mergeChunkTranscriptions — degraded text strategy', () => {
  it('removes a known five-word overlap', () => {
    const result = mergeChunkTranscriptions(
      textOnlyInputs([
        'one two three four five six seven eight nine ten',
        'six seven eight nine ten eleven twelve',
      ]),
    );

    expect(result.strategy).toBe('text');
    expect(result.degraded).toBe(true);
    expect(result.segments).toEqual([]);
    expect(result.text).toBe('one two three four five six seven eight nine ten eleven twelve');
    expectTidy(result.text);
  });

  it('matches across differing casing and punctuation', () => {
    const result = mergeChunkTranscriptions(
      textOnlyInputs([
        'the meeting ended and it was, indeed, remarkable.',
        'It was indeed remarkable. And then we left.',
      ]),
    );

    expect(result.text).toBe('the meeting ended and it was, indeed, remarkable. And then we left.');
    expectTidy(result.text);
  });

  it('does not treat a single coincidental word as an overlap', () => {
    const result = mergeChunkTranscriptions(
      textOnlyInputs(['alpha beta gamma the', 'the delta epsilon']),
    );

    expect(result.text).toBe('alpha beta gamma the the delta epsilon');
  });

  it('joins with a single space when there is no overlap at all', () => {
    const result = mergeChunkTranscriptions(
      textOnlyInputs(['completely different opening', 'entirely unrelated ending']),
    );

    expect(result.text).toBe('completely different opening entirely unrelated ending');
    expectTidy(result.text);
  });

  it('stays well-formed when a middle chunk came back empty', () => {
    const result = mergeChunkTranscriptions(
      textOnlyInputs(['first chunk speech', '   ', 'third chunk speech']),
    );

    expect(result.text).toBe('first chunk speech third chunk speech');
    expectTidy(result.text);
  });

  it('returns an empty string for silence across every chunk', () => {
    const result = mergeChunkTranscriptions(textOnlyInputs(['', '  ', '']));

    expect(result.text).toBe('');
    expect(result.strategy).toBe('text');
    expect(result.degraded).toBe(true);
  });

  it('de-duplicates a long overlap window without dropping later speech', () => {
    const shared = Array.from({ length: 12 }, (_, i) => `shared${i}`).join(' ');
    const result = mergeChunkTranscriptions(
      textOnlyInputs([`lead in ${shared}`, `${shared} trailing words here`]),
    );

    expect(result.text).toBe(`lead in ${shared} trailing words here`);
    expectTidy(result.text);
  });

  it('merges out-of-order text chunks in timeline order', () => {
    const inputs = textOnlyInputs(['alpha one', 'beta two', 'gamma three']);
    const result = mergeChunkTranscriptions([inputs[2], inputs[0], inputs[1]]);

    expect(result.text).toBe('alpha one beta two gamma three');
  });
});

describe('mergeChunkTranscriptions — invalid input', () => {
  it('throws on an empty input array', () => {
    expect(() => mergeChunkTranscriptions([])).toThrow(
      'mergeChunkTranscriptions requires at least one chunk transcription',
    );
  });

  it('throws on duplicate chunk indices', () => {
    const [plan] = planAudioChunks(60);
    expect(() =>
      mergeChunkTranscriptions([
        { plan, transcription: { text: 'a' } },
        { plan, transcription: { text: 'b' } },
      ]),
    ).toThrow('duplicate chunk index 0');
  });
});

describe('mergeChunkTranscriptions — malformed provider data', () => {
  it('tolerates null entries, missing words and NaN timestamps', () => {
    const [first, last] = planAudioChunks(60);
    const junkWords = [
      null,
      undefined,
      { word: 'kept', start: 1, end: 2 },
      { start: 3, end: 4 },
      { word: 'nanned', start: NaN, end: NaN },
      { word: '', start: 5, end: 6 },
    ] as unknown as TranscriptWord[];

    const result = mergeChunkTranscriptions([
      { plan: first, transcription: { text: 'kept', words: junkWords } },
      {
        plan: last,
        transcription: {
          text: 'later',
          words: [{ word: 'later', start: 12, end: 13 }, null as unknown as TranscriptWord],
          segments: [] as TranscriptSegment[],
        },
      },
    ]);

    expect(result.strategy).toBe('words');
    // The NaN-timestamped word cannot be placed on the timeline, but it is on the first chunk,
    // so it is preserved rather than silently dropped.
    expect(result.text).toBe('kept nanned later');
    expectTidy(result.text);
  });

  it('drops untimestamped words on later chunks so they are never duplicated', () => {
    const [first, last] = planAudioChunks(60);
    const result = mergeChunkTranscriptions([
      {
        plan: first,
        transcription: {
          text: 'anchor floating',
          words: [{ word: 'anchor', start: 1, end: 2 }, { word: 'floating' }],
        },
      },
      {
        plan: last,
        transcription: {
          text: 'floating tail',
          words: [{ word: 'floating' }, { word: 'tail', start: 12, end: 13 }],
        },
      },
    ]);

    expect(result.text).toBe('anchor floating tail');
    expect(result.text.split(' ').filter((w) => w === 'floating')).toHaveLength(1);
  });

  it('degrades to text when segments and words arrays are present but empty', () => {
    const inputs = planAudioChunks(60).map((plan, i) => ({
      plan,
      transcription: { text: i === 0 ? 'only text here' : 'text here and more', words: [] },
    }));

    const result = mergeChunkTranscriptions(inputs);

    expect(result.strategy).toBe('text');
    expect(result.degraded).toBe(true);
    expect(result.text).toBe('only text here and more');
    expectTidy(result.text);
  });

  it('never throws and never returns undefined text for a missing transcription', () => {
    const inputs = planAudioChunks(60).map((plan) => ({
      plan,
      transcription: undefined as unknown as { text: string },
    }));

    const result = mergeChunkTranscriptions(inputs);

    expect(result.text).toBe('');
    expect(result.segments).toEqual([]);
  });
});
