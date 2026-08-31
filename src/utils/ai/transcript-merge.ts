// src/utils/ai/transcript-merge.ts — Pure merge of per-chunk Groq transcriptions.
//
// Overlapping uploads mean the same speech is transcribed twice at every boundary. The
// cheap-looking fix — glue the texts together and reconcile the duplicate afterwards —
// requires choosing between two competing recognitions of the same words, which is a
// judgement call the merge has no information to make: the two readings usually differ in
// casing, punctuation and sometimes wording, so any diff-based reconciliation either drops
// real speech or leaves a stutter.
//
// Instead, ownership is decided by *geometry* before any text is compared. `planAudioChunks`
// already partitioned the timeline into non-overlapping cores, and a token belongs to the chunk
// whose core contains its midpoint. The two chunks either side of a boundary do not share a
// clock, though: each is recognised independently and Whisper never returns quite the same span
// twice, so a token sitting on a core edge can land either side of it by a few tens of
// milliseconds. A strict core test therefore fails in both directions — claimed twice, or by
// nobody at all. So each edge is claimed *inclusively*, with a tolerance band taken by both
// neighbours: duplication becomes the only error the claim step can make, and it never drops
// speech.
//
// The duplicate is then removed at each seam with the same normalized suffix/prefix match the
// degraded text path uses. When the two recognitions disagree on wording nothing matches and both
// readings survive — a stutter confined to the tolerance band, which a reader can recover from,
// unlike missing speech. The dedupe carries one bounded risk of its own, named on
// `appendAcrossSeam`. Order stays deterministic regardless of request completion order.
//
// Post-hoc text reconciliation survives only as the `'text'` strategy, used when the provider
// returned no usable timestamps at all. It is lossy, so it sets `degraded: true`.

import type { AudioChunkPlan } from './audio-chunk-plan';

export interface TranscriptWord {
  word?: string;
  start?: number;
  end?: number;
}

export interface TranscriptSegment {
  id?: number;
  start?: number;
  end?: number;
  text?: string;
  avg_logprob?: number;
  no_speech_prob?: number;
  compression_ratio?: number;
}

export interface ChunkTranscription {
  text: string;
  words?: TranscriptWord[];
  segments?: TranscriptSegment[];
}

export interface ChunkMergeInput {
  plan: AudioChunkPlan;
  transcription: ChunkTranscription;
}

export type MergeStrategy = 'words' | 'segments' | 'text' | 'single';

export interface MergeResult {
  text: string;
  /** Retained segments only, rebased onto the original timeline. */
  segments: TranscriptSegment[];
  strategy: MergeStrategy;
  /** True when the degraded text-only path was used for at least one boundary. */
  degraded: boolean;
}

/** Boundary window, in words, compared on each side when de-duplicating raw text. */
const TEXT_WINDOW_WORDS = 50;
/** Shorter matches than this are coincidence ("the", "and the"), not a real overlap. */
const MIN_TEXT_OVERLAP_WORDS = 2;
/**
 * Word-timestamp drift tolerated on each side of a core edge before ownership is decided. Well
 * above the tens of milliseconds two independent Whisper passes disagree by, and well under the
 * 2.5s of shared audio each side of a boundary actually holds — so the band can never reach past
 * the overlap into speech only one chunk ever heard.
 */
const BOUNDARY_TOLERANCE_SECONDS = 0.5;

const NO_SPACE_BEFORE = new Set([',', '.', '!', '?', ';', ':', '%', ')', ']', '}']);
const NO_SPACE_AFTER = new Set(['(', '[', '{', '$']);

// Millisecond precision, matching audio-chunk-plan, so rebased timestamps stay tidy instead of
// accumulating float noise like 28.499999999999996.
const PRECISION = 1_000;

function isNum(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function shift(value: unknown, offset: number): number | undefined {
  return isNum(value) ? Math.round((value + offset) * PRECISION) / PRECISION : undefined;
}

function collapse(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function asArray<T>(value: T[] | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

/** Joins tokens with single spaces while respecting punctuation that must hug its neighbour. */
function joinWords(tokens: string[]): string {
  let out = '';
  for (const raw of tokens) {
    const token = typeof raw === 'string' ? raw.trim() : '';
    if (!token) continue;
    if (!out) {
      out = token;
      continue;
    }
    const glue = NO_SPACE_BEFORE.has(token[0]) || NO_SPACE_AFTER.has(out[out.length - 1]);
    out += glue ? token : ` ${token}`;
  }
  return collapse(out);
}

function normalize(value: string): string {
  return collapse(value.toLowerCase().replace(/\p{P}/gu, ' '));
}

function rebaseSegment(segment: TranscriptSegment, offset: number): TranscriptSegment {
  return { ...segment, start: shift(segment.start, offset), end: shift(segment.end, offset) };
}

/** Length of the longest normalized suffix/prefix match, or 0 when there is no real overlap. */
function overlapWordCount(
  accWords: string[],
  nextWords: string[],
  minWords = MIN_TEXT_OVERLAP_WORDS,
): number {
  const tail = accWords.slice(-TEXT_WINDOW_WORDS);
  const head = nextWords.slice(0, TEXT_WINDOW_WORDS);
  const max = Math.min(TEXT_WINDOW_WORDS, tail.length, head.length);
  for (let k = max; k >= minWords; k -= 1) {
    const left = normalize(tail.slice(tail.length - k).join(' '));
    const right = normalize(head.slice(0, k).join(' '));
    if (left && left === right) return k;
  }
  return 0;
}

/**
 * Midpoint ownership: the token belongs to this chunk when its centre falls in the chunk's core,
 * widened by `BOUNDARY_TOLERANCE_SECONDS` at both edges. Inclusive at both ends on purpose — a
 * token whose two recognitions straddle a core edge is claimed by *both* neighbours rather than
 * risking neither, and the seam dedupe drops the surplus copy.
 */
function ownsMidpoint(start: number, end: number, plan: AudioChunkPlan): boolean {
  const mid = (start + end) / 2;
  return (
    mid >= plan.coreStartSeconds - BOUNDARY_TOLERANCE_SECONDS &&
    mid <= plan.coreEndSeconds + BOUNDARY_TOLERANCE_SECONDS
  );
}

function hasUsableWords(transcription: ChunkTranscription): boolean {
  const words = asArray(transcription.words);
  return words.length > 0 && words.some((w) => !!w && isNum(w.start) && isNum(w.end));
}

function hasUsableSegments(transcription: ChunkTranscription): boolean {
  const segments = asArray(transcription.segments);
  return segments.length > 0 && segments.some((s) => !!s && isNum(s.start) && isNum(s.end));
}

/**
 * A chunk that carries nothing at all — silence, or a request that came back empty. Text without
 * timestamps is deliberately *not* vacuous: it cannot be placed geometrically, so it still has to
 * demote the whole merge to the text path.
 */
function isVacuous(transcription: ChunkTranscription): boolean {
  if (hasUsableWords(transcription) || hasUsableSegments(transcription)) return false;
  return collapse(typeof transcription.text === 'string' ? transcription.text : '') === '';
}

/** Tokens this chunk owns, in timeline order. */
function ownedWords(
  transcription: ChunkTranscription,
  plan: AudioChunkPlan,
  isFirst: boolean,
): string[] {
  const tokens: string[] = [];
  for (const word of asArray(transcription.words)) {
    if (!word || typeof word.word !== 'string') continue;
    const start = shift(word.start, plan.startSeconds);
    const end = shift(word.end, plan.startSeconds);
    if (start === undefined || end === undefined) {
      // No timestamps means no midpoint, so neither the tolerance band nor the seam dedupe can
      // reason about the token. Keeping it only on the first chunk emits it once without any
      // chance of a copy surfacing inside a neighbour's core.
      if (isFirst) tokens.push(word.word);
      continue;
    }
    if (ownsMidpoint(start, end, plan)) tokens.push(word.word);
  }
  return tokens;
}

/** Segments this chunk owns, rebased onto the global timeline. */
function ownedSegments(
  transcription: ChunkTranscription,
  plan: AudioChunkPlan,
): TranscriptSegment[] {
  const owned: TranscriptSegment[] = [];
  for (const segment of asArray(transcription.segments)) {
    if (!segment) continue;
    const start = shift(segment.start, plan.startSeconds);
    const end = shift(segment.end, plan.startSeconds);
    // A segment without both timestamps cannot be placed; skipping it only loses quality
    // metadata, never text (text comes from the words path in that case).
    if (start === undefined || end === undefined) continue;
    if (!ownsMidpoint(start, end, plan)) continue;
    owned.push({ ...segment, start, end });
  }
  return owned;
}

/**
 * Appends the next chunk's tokens, dropping the boundary speech the widened claim deliberately
 * duplicated. A single-token match counts here — geometry, not chance, put these tokens on both
 * sides, and the two-word coincidence guard the blind text path needs would leave the common
 * one-word straddle in place. Residual risk: with no duplicate at all (a silence spanning the
 * band) two coincidentally equal words at the seam cost one word; a duplicated word every seam
 * is the worse trade.
 */
function appendAcrossSeam(acc: string[], next: string[]): void {
  if (next.length === 0) return;
  const duplicated = acc.length === 0 ? 0 : overlapWordCount(acc, next, 1);
  acc.push(...next.slice(duplicated));
}

function segmentKey(segment: TranscriptSegment | undefined): string {
  return normalize(typeof segment?.text === 'string' ? segment.text : '');
}

/**
 * Same seam as `appendAcrossSeam`, but segments carry timestamps that must stay truthful, so this
 * only ever drops a whole leading segment that repeats the previous chunk's trailing one verbatim
 * (modulo case and punctuation). No partial reconciliation, no re-timing.
 */
function appendSegmentsAcrossSeam(acc: TranscriptSegment[], next: TranscriptSegment[]): void {
  const previous = segmentKey(acc[acc.length - 1]);
  const duplicated = previous !== '' && previous === segmentKey(next[0]);
  acc.push(...(duplicated ? next.slice(1) : next));
}

function allSegments(inputs: ChunkMergeInput[]): TranscriptSegment[] {
  const out: TranscriptSegment[] = [];
  for (const { plan, transcription } of inputs) {
    for (const segment of asArray(transcription.segments)) {
      if (!segment) continue;
      out.push(rebaseSegment(segment, plan.startSeconds));
    }
  }
  return out;
}

function mergeByWords(inputs: ChunkMergeInput[]): MergeResult {
  const tokens: string[] = [];
  const segments: TranscriptSegment[] = [];

  inputs.forEach(({ plan, transcription }, position) => {
    appendAcrossSeam(tokens, ownedWords(transcription, plan, position === 0));
    appendSegmentsAcrossSeam(segments, ownedSegments(transcription, plan));
  });

  return { text: joinWords(tokens), segments, strategy: 'words', degraded: false };
}

function mergeBySegments(inputs: ChunkMergeInput[]): MergeResult {
  const segments: TranscriptSegment[] = [];
  inputs.forEach(({ plan, transcription }) => {
    appendSegmentsAcrossSeam(segments, ownedSegments(transcription, plan));
  });

  const text = collapse(
    segments
      .map((segment) => (typeof segment.text === 'string' ? segment.text.trim() : ''))
      .filter(Boolean)
      .join(' '),
  );

  return { text, segments, strategy: 'segments', degraded: false };
}

function mergeByText(inputs: ChunkMergeInput[]): MergeResult {
  let acc = '';
  for (const { transcription } of inputs) {
    const next = collapse(typeof transcription.text === 'string' ? transcription.text : '');
    if (!next) continue;
    if (!acc) {
      acc = next;
      continue;
    }
    const nextWords = next.split(' ');
    const kept = nextWords.slice(overlapWordCount(acc.split(' '), nextWords)).join(' ');
    acc = collapse(kept ? `${acc} ${kept}` : acc);
  }

  return { text: acc, segments: allSegments(inputs), strategy: 'text', degraded: true };
}

export function mergeChunkTranscriptions(inputs: ChunkMergeInput[]): MergeResult {
  if (!Array.isArray(inputs) || inputs.length === 0) {
    throw new Error('mergeChunkTranscriptions requires at least one chunk transcription');
  }

  const seen = new Set<number>();
  for (const input of inputs) {
    const index = input.plan.index;
    if (seen.has(index)) {
      throw new Error(`mergeChunkTranscriptions received duplicate chunk index ${index}`);
    }
    seen.add(index);
  }

  // Completion order is arbitrary once chunks are transcribed concurrently; timeline order is not.
  const ordered = [...inputs]
    .sort((a, b) => a.plan.index - b.plan.index)
    .map((input) => ({ plan: input.plan, transcription: input.transcription ?? { text: '' } }));

  if (ordered.length === 1) {
    const { text } = ordered[0].transcription;
    return {
      text: collapse(typeof text === 'string' ? text : ''),
      segments: allSegments(ordered),
      strategy: 'single',
      degraded: false,
    };
  }

  // A vacuous chunk is admitted to either timestamped path because it contributes nothing to it
  // either way — one silent minute in forty says nothing about the other thirty-nine. At least
  // one chunk must still carry the timestamps, or there is no geometry to trust and an all-silent
  // recording would claim a strategy it never exercised.
  const parts = ordered.map(({ transcription }) => transcription);
  if (parts.some(hasUsableWords) && parts.every((t) => hasUsableWords(t) || isVacuous(t))) {
    return mergeByWords(ordered);
  }
  if (parts.some(hasUsableSegments) && parts.every((t) => hasUsableSegments(t) || isVacuous(t))) {
    return mergeBySegments(ordered);
  }
  return mergeByText(ordered);
}
