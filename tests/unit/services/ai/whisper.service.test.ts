// tests/unit/services/ai/whisper.service.test.ts — long-audio transcription pipeline.
//
// Seams that are mocked: the Groq client (`openai`), `AudioConverter.prepare`, `global.fetch`
// and `fs/promises.mkdtemp` (wrapping the real implementation so temp-directory cleanup is a
// real filesystem assertion). Everything else — the streamed download, the worker pool, the
// retry ladder, the merge and the quality pass — runs for real.
//
// Determinism: `Date.now` is a fake clock that only the injected `retrySleep` advances, so
// deadlines, backoff and the shared limiter cooldown are all observable without timers.

const mockTranscriptionsCreate: jest.Mock = jest.fn();

jest.mock('openai', () =>
  jest.fn().mockImplementation(() => ({
    audio: { transcriptions: { create: mockTranscriptionsCreate } },
  })),
);

jest.mock('fs/promises', () => {
  const actual = jest.requireActual('fs/promises');
  return { ...actual, mkdtemp: jest.fn(actual.mkdtemp) };
});

import { existsSync, writeFileSync } from 'fs';
import { mkdtemp } from 'fs/promises';
import { dirname, join } from 'path';

import { WhisperService, WhisperConfig } from '../../../../src/services/ai/whisper.service';
import { GroqTranscriptionError } from '../../../../src/services/ai/groq-transcription-error';
import { AudioAdmissionError } from '../../../../src/utils/ai/audio-admission-error';
import {
  AudioConverter,
  PrepareAudioOptions,
  PreparedAudio,
  PreparedAudioChunk,
} from '../../../../src/utils/ai/audioConverter';
import { planAudioChunks } from '../../../../src/utils/ai/audio-chunk-plan';
import {
  resolveAudioPrepareTimeoutMs,
  resolveAudioTranscriptionTimeoutMs,
} from '../../../../src/config/turn-timeout.config';
import type { TranscriptSegment, TranscriptWord } from '../../../../src/utils/ai/transcript-merge';
import { logger } from '../../../../src/utils/logger';

const mockedMkdtemp = mkdtemp as jest.MockedFunction<typeof mkdtemp>;

const FILE_URL = 'https://api.telegram.org/file/bottoken/voice/file.ogg';
// Chunk files are `CHUNK_BASE_BYTES + index` bytes long, which is how the Groq mock recovers
// the chunk index from the uploaded File without depending on call order.
const CHUNK_BASE_BYTES = 64;

interface VerboseJson {
  text: string;
  language?: string;
  words?: TranscriptWord[];
  segments?: TranscriptSegment[];
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function verboseJson(overrides: Partial<VerboseJson> = {}): VerboseJson {
  return {
    text: 'transcribed text',
    language: 'en',
    segments: [
      {
        id: 0,
        start: 0,
        end: 1.4,
        text: 'transcribed text',
        avg_logprob: -0.1,
        no_speech_prob: 0.01,
        compression_ratio: 1.2,
      },
    ],
    ...overrides,
  };
}

// Script for a 60-second, two-chunk run: cores 0–30 / 30–60, uploads 0–32.5 / 27.5–60.
// Each segment's chunk-local timing rebases into its own core, so both survive the merge and
// the merged text is 'first half second half'.
function twoChunkScript(index: number): VerboseJson {
  const segment =
    index === 0
      ? { id: 0, start: 1, end: 2, text: 'first half' }
      : { id: 1, start: 12.5, end: 13.5, text: 'second half' };

  return {
    text: segment.text,
    language: 'en',
    segments: [{ ...segment, avg_logprob: -0.1, no_speech_prob: 0.02, compression_ratio: 1.2 }],
  };
}

function groqApiError(status: number, retryAfter?: string): Error {
  const headers = new Headers({
    'x-request-id': `groq-${status}`,
    'x-ratelimit-remaining-requests': '9',
  });
  if (retryAfter !== undefined) headers.set('retry-after', retryAfter);
  return Object.assign(new Error(`Groq returned ${status}`), {
    status,
    headers,
    requestID: `groq-${status}`,
    type: status === 429 ? 'rate_limit_error' : 'api_error',
  });
}

// Drains microtasks plus one macrotask turn (fs and stream callbacks need the latter).
async function turn(count = 3): Promise<void> {
  for (let index = 0; index < count; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

async function waitFor(predicate: () => boolean, label: string): Promise<void> {
  for (let index = 0; index < 300; index += 1) {
    if (predicate()) return;
    await turn(1);
  }
  throw new Error(`waitFor timed out: ${label}`);
}

describe('WhisperService', () => {
  const originalEnv = process.env;
  const originalFetch = global.fetch;
  let dateNowSpy: jest.SpyInstance<number, []>;
  let prepareSpy: jest.SpyInstance<Promise<PreparedAudio>, [PrepareAudioOptions]>;

  // Fake clock: only `retrySleep` (and therefore the limiter cooldown sleep) moves it.
  let clock: number;
  let retrySleep: jest.Mock<Promise<void>, [number]>;
  /** When set, `retrySleep` parks instead of returning, so cooldown windows are observable. */
  let sleepGate: Array<{ ms: number; release: () => void }> | undefined;

  // Prepared-audio shape for the run under test.
  let preparedDurationSeconds: number;
  let chunkSizeBytesOverride: number | undefined;
  let prepareOutcome: ((options: PrepareAudioOptions) => Promise<PreparedAudio>) | undefined;

  // Groq responses, keyed by chunk index. `respond` may be called more than once per index
  // when a chunk is retried.
  let respond: (index: number, attempt: number) => Promise<VerboseJson>;
  let createdChunkIndexes: number[];
  let attemptsByChunk: Map<number, number>;
  let outstandingCreates: number;
  let peakOutstandingCreates: number;

  const loggerMock = logger as jest.Mocked<typeof logger>;

  function buildPrepared(options: PrepareAudioOptions): PreparedAudio {
    const plans = planAudioChunks(preparedDurationSeconds, {
      coreSeconds: options.coreSeconds,
      overlapSeconds: options.overlapSeconds,
    });

    const chunks: PreparedAudioChunk[] = plans.map((plan) => {
      const path = join(options.workDir, `chunk-${String(plan.index).padStart(3, '0')}.flac`);
      const bytes = Buffer.alloc(CHUNK_BASE_BYTES + plan.index, plan.index);
      writeFileSync(path, bytes);
      return { ...plan, path, sizeBytes: chunkSizeBytesOverride ?? bytes.length };
    });

    return {
      durationSeconds: preparedDurationSeconds,
      normalizedPath: join(options.workDir, 'normalized.flac'),
      chunks,
      normalizedSizeBytes: 4_096,
      prepareTimeMs: 25,
    };
  }

  function mockDownload(
    options: {
      bytes?: number;
      contentLength?: number | null;
      contentType?: string | null;
      ok?: boolean;
      status?: number;
      statusText?: string;
      body?: 'stream' | 'empty' | 'null';
    } = {},
  ): void {
    const {
      bytes = 1_024,
      contentLength = null,
      contentType = 'audio/ogg',
      ok = true,
      status = 200,
      statusText = 'OK',
      body = 'stream',
    } = options;

    global.fetch = jest.fn(async () => {
      const headers = new Headers();
      if (contentType) headers.set('content-type', contentType);
      if (contentLength !== null) headers.set('content-length', String(contentLength));

      const stream =
        body === 'null'
          ? null
          : new ReadableStream<Uint8Array>({
              start(controller) {
                if (body === 'stream') controller.enqueue(new Uint8Array(bytes));
                controller.close();
              },
            });

      return { ok, status, statusText, headers, body: stream } as unknown as Response;
    }) as unknown as typeof fetch;
  }

  function makeService(overrides: Partial<WhisperConfig> = {}): WhisperService {
    return new WhisperService({
      apiKey: 'groq-test-key',
      maxInputBytes: 4_096,
      maxDurationSeconds: 1_200,
      coreSeconds: 30,
      overlapSeconds: 5,
      maxConcurrentRequests: 5,
      retrySleep,
      retryRandom: () => 1,
      ...overrides,
    });
  }

  /** The mkdtemp directory created by the run under test. */
  async function workDir(): Promise<string> {
    expect(mockedMkdtemp.mock.results.length).toBeGreaterThan(0);
    return (await mockedMkdtemp.mock.results[0].value) as string;
  }

  function logPayloads(): unknown[] {
    return [
      ...loggerMock.info.mock.calls,
      ...loggerMock.warn.mock.calls,
      ...loggerMock.error.mock.calls,
      ...loggerMock.debug.mock.calls,
    ];
  }

  function loggedEvent(event: string): Record<string, unknown> | undefined {
    for (const calls of [
      loggerMock.info.mock.calls,
      loggerMock.warn.mock.calls,
      loggerMock.error.mock.calls,
      loggerMock.debug.mock.calls,
    ]) {
      const match = calls.find((call) => call[0] === event);
      if (match) return match[1] as Record<string, unknown>;
    }
    return undefined;
  }

  beforeEach(() => {
    process.env = { ...originalEnv };
    delete process.env.GROQ_API_KEY;
    // The two audio budgets are read from the environment by the shared ladder resolvers,
    // so a developer's exported override would otherwise move every deadline asserted here.
    delete process.env.GROQ_AUDIO_PREPARE_TIMEOUT_MS;
    delete process.env.GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS;

    clock = 1_700_000_000_000;
    dateNowSpy = jest.spyOn(Date, 'now').mockImplementation(() => clock);

    sleepGate = undefined;
    retrySleep = jest.fn((ms: number) => {
      if (!sleepGate) {
        clock += ms;
        return Promise.resolve();
      }
      const gate = sleepGate;
      return new Promise<void>((resolve) => {
        gate.push({
          ms,
          release: () => {
            clock += ms;
            resolve();
          },
        });
      });
    });

    preparedDurationSeconds = 10;
    chunkSizeBytesOverride = undefined;
    prepareOutcome = undefined;
    prepareSpy = jest
      .spyOn(AudioConverter, 'prepare')
      .mockImplementation(async (options: PrepareAudioOptions) =>
        prepareOutcome ? prepareOutcome(options) : buildPrepared(options),
      );

    createdChunkIndexes = [];
    attemptsByChunk = new Map();
    outstandingCreates = 0;
    peakOutstandingCreates = 0;
    respond = async (index) => verboseJson({ text: `chunk ${index} text` });

    mockTranscriptionsCreate.mockImplementation((params: { file: File }) => {
      const index = params.file.size - CHUNK_BASE_BYTES;
      const attempt = (attemptsByChunk.get(index) ?? 0) + 1;
      attemptsByChunk.set(index, attempt);
      createdChunkIndexes.push(index);
      outstandingCreates += 1;
      peakOutstandingCreates = Math.max(peakOutstandingCreates, outstandingCreates);

      const settled = Promise.resolve()
        .then(() => respond(index, attempt))
        .then(
          (data) => {
            outstandingCreates -= 1;
            return {
              data,
              response: { status: 200, headers: new Headers() },
              request_id: `req_${index}_${attempt}`,
            };
          },
          (error) => {
            outstandingCreates -= 1;
            throw error;
          },
        );

      return { withResponse: () => settled };
    });

    mockDownload();
  });

  afterEach(() => {
    process.env = originalEnv;
    global.fetch = originalFetch;
    dateNowSpy.mockRestore();
    prepareSpy.mockRestore();
    mockTranscriptionsCreate.mockReset();
  });

  // -------------------------------------------------------------------------
  // Admission and download
  // -------------------------------------------------------------------------

  describe('admission and download', () => {
    it('accepts a declared content-length exactly at the input limit', async () => {
      mockDownload({ contentLength: 4_096, bytes: 4_096 });

      await expect(makeService().transcribeAudio(FILE_URL)).resolves.toMatchObject({
        chunkCount: 1,
      });
      expect(prepareSpy).toHaveBeenCalledTimes(1);
    });

    it('rejects a declared content-length one byte over the limit without preparing', async () => {
      mockDownload({ contentLength: 4_097, bytes: 4_097 });

      const failure = await makeService()
        .transcribeAudio(FILE_URL)
        .catch((error: unknown) => error);

      expect(failure).toBeInstanceOf(AudioAdmissionError);
      expect((failure as AudioAdmissionError).reason).toBe('too_large');
      expect((failure as AudioAdmissionError).limit).toBe(4_096);
      expect((failure as AudioAdmissionError).observed).toBe(4_097);
      expect(prepareSpy).not.toHaveBeenCalled();
      expect(mockTranscriptionsCreate).not.toHaveBeenCalled();
    });

    it('catches a lying (absent) content-length with the streaming byte counter', async () => {
      mockDownload({ contentLength: null, bytes: 4_097 });

      const failure = await makeService()
        .transcribeAudio(FILE_URL)
        .catch((error: unknown) => error);

      expect(failure).toBeInstanceOf(AudioAdmissionError);
      expect((failure as AudioAdmissionError).reason).toBe('too_large');
      expect(prepareSpy).not.toHaveBeenCalled();
      expect(mockTranscriptionsCreate).not.toHaveBeenCalled();
    });

    it('reports a non-ok response as a download failure', async () => {
      mockDownload({ ok: false, status: 403, statusText: 'Forbidden' });

      const failure = (await makeService()
        .transcribeAudio(FILE_URL)
        .catch((error: unknown) => error)) as Error;

      // transcribeAudio wraps the download error once for the caller.
      expect(failure.message).toBe(
        'Transcription failed: Failed to download audio file: HTTP 403: Forbidden',
      );
      const cause = (failure as Error & { cause?: Error }).cause as Error;
      expect(cause.message.startsWith('Failed to download audio file:')).toBe(true);
      expect(prepareSpy).not.toHaveBeenCalled();
    });

    it('reports an empty body as a download failure', async () => {
      mockDownload({ body: 'empty' });

      await expect(makeService().transcribeAudio(FILE_URL)).rejects.toThrow(
        /Failed to download audio file: Downloaded audio file is empty/,
      );
      expect(prepareSpy).not.toHaveBeenCalled();
    });

    it('reports a missing body as a download failure', async () => {
      mockDownload({ body: 'null' });

      await expect(makeService().transcribeAudio(FILE_URL)).rejects.toThrow(
        /Failed to download audio file: Response body was empty/,
      );
      expect(prepareSpy).not.toHaveBeenCalled();
    });

    it('removes the work directory after a download failure', async () => {
      mockDownload({ ok: false, status: 500, statusText: 'Server Error' });

      await expect(makeService().transcribeAudio(FILE_URL)).rejects.toThrow();

      const dir = await workDir();
      expect(dir).toContain('jarvis-audio-');
      expect(existsSync(dir)).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Prepare
  // -------------------------------------------------------------------------

  describe('prepare', () => {
    it('passes the configured limits and a work-directory input path to AudioConverter', async () => {
      const service = makeService({
        maxDurationSeconds: 900,
        coreSeconds: 25,
        overlapSeconds: 4,
        maxFileSizeBytes: 1_000_000,
        prepareTimeoutMs: 45_000,
      });

      await service.transcribeAudio(FILE_URL, 42, { requestId: 'tg_prepare' });

      const options = prepareSpy.mock.calls[0][0];
      const dir = await workDir();
      expect(options).toMatchObject({
        workDir: dir,
        maxDurationSeconds: 900,
        coreSeconds: 25,
        overlapSeconds: 4,
        maxChunkBytes: 1_000_000,
        timeoutMs: 45_000,
        userId: 42,
        logContext: { requestId: 'tg_prepare' },
      });
      expect(dirname(options.inputPath)).toBe(dir);
      expect(options.inputPath).toContain('input.ogg');
    });

    it('propagates an AudioAdmissionError from prepare unchanged', async () => {
      const admission = new AudioAdmissionError('too_long', { observed: 1_500, limit: 1_200 });
      prepareOutcome = async () => {
        throw admission;
      };

      const failure = await makeService()
        .transcribeAudio(FILE_URL)
        .catch((error: unknown) => error);

      expect(failure).toBe(admission);
      expect((failure as AudioAdmissionError).reason).toBe('too_long');
      expect((failure as Error).message).not.toContain('Transcription failed:');
      expect(existsSync(await workDir())).toBe(false);
    });

    it('wraps a generic prepare failure and removes the work directory', async () => {
      prepareOutcome = async () => {
        throw new Error('Audio preparation failed: no audio stream found');
      };

      await expect(makeService().transcribeAudio(FILE_URL)).rejects.toThrow(
        'Transcription failed: Audio preparation failed: no audio stream found',
      );
      expect(existsSync(await workDir())).toBe(false);
      expect(mockTranscriptionsCreate).not.toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // Short audio: one chunk
  // -------------------------------------------------------------------------

  describe('short audio', () => {
    it('issues one FLAC verbose_json request and returns a single-chunk result', async () => {
      preparedDurationSeconds = 10;
      respond = async () => verboseJson({ text: 'Add project review tomorrow' });

      const result = await makeService().transcribeAudio(FILE_URL);

      expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(1);
      expect(result).toMatchObject({
        text: 'Add project review tomorrow',
        chunkCount: 1,
        durationSeconds: 10,
        fileSizeBytes: 4_096,
        detectedLanguage: 'en',
        fileUrl: FILE_URL,
      });

      const request = mockTranscriptionsCreate.mock.calls[0][0];
      expect(request.file.name).toMatch(/\.flac$/);
      expect(request.file.type).toBe('audio/flac');
      expect(request).toMatchObject({
        model: 'whisper-large-v3',
        language: 'en',
        response_format: 'verbose_json',
        temperature: 0,
      });
      expect(request.timestamp_granularities).toEqual(expect.arrayContaining(['segment', 'word']));
    });

    it('uses requestTimeoutMs for one chunk and chunkRequestTimeoutMs beyond that', async () => {
      preparedDurationSeconds = 10;
      await makeService().transcribeAudio(FILE_URL);
      expect(mockTranscriptionsCreate.mock.calls[0][1]).toMatchObject({
        timeout: 12_000,
        maxRetries: 0,
      });

      mockTranscriptionsCreate.mockClear();
      mockedMkdtemp.mockClear();
      preparedDurationSeconds = 60; // two chunks -> long-form budgets

      await makeService().transcribeAudio(FILE_URL);

      expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(2);
      for (const call of mockTranscriptionsCreate.mock.calls) {
        expect(call[1]).toMatchObject({ timeout: 60_000, maxRetries: 0 });
      }
    });
  });

  // -------------------------------------------------------------------------
  // Long form: concurrency, ordering, retries
  // -------------------------------------------------------------------------

  describe('long-form concurrency', () => {
    it('never exceeds the concurrency limit for one job of twelve chunks', async () => {
      preparedDurationSeconds = 360; // 12 chunks at 30s cores
      const gates = new Map<number, Deferred<VerboseJson>>();
      respond = (index) => {
        const gate = deferred<VerboseJson>();
        gates.set(index, gate);
        return gate.promise;
      };

      const run = makeService({ maxConcurrentRequests: 5 }).transcribeAudio(FILE_URL);

      for (let released = 0; released < 12;) {
        const expected = Math.min(5, 12 - released);
        await waitFor(() => gates.size >= expected, `${expected} chunk requests dispatched`);
        // Let every worker that could still dispatch do so before measuring.
        await turn(5);
        expect(outstandingCreates).toBeLessThanOrEqual(5);
        for (const [index, gate] of [...gates]) {
          gates.delete(index);
          gate.resolve(verboseJson({ text: `chunk ${index}` }));
          released += 1;
        }
        await turn();
      }

      await run;
      expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(12);
      expect(peakOutstandingCreates).toBeLessThanOrEqual(5);
      expect(peakOutstandingCreates).toBe(5);
    });

    // The point of the shared limiter: two jobs on the same instance field ten pool workers
    // between them (5 each), yet only five Groq requests may be in flight at any moment.
    it('shares the concurrency limit across two simultaneous jobs on one instance', async () => {
      preparedDurationSeconds = 180; // 6 chunks per job -> 5 workers each
      const gates: Array<Deferred<VerboseJson>> = [];
      respond = () => {
        const gate = deferred<VerboseJson>();
        gates.push(gate);
        return gate.promise;
      };

      const service = makeService({ maxConcurrentRequests: 5 });
      const jobs = [service.transcribeAudio(FILE_URL, 1)];
      // Second job joins once the first has filled the slots, which is both the realistic
      // shape and deterministic — no dependence on how two downloads interleave.
      await waitFor(() => gates.length === 5, 'first job saturated the limiter');
      jobs.push(service.transcribeAudio(FILE_URL, 2));

      // Both jobs must be past prepare — i.e. all ten workers live — before measuring.
      await waitFor(() => prepareSpy.mock.calls.length === 2, 'both jobs prepared');
      await turn(5);
      expect(outstandingCreates).toBe(5);

      for (let released = 0; released < 12;) {
        const expected = Math.min(5, 12 - released);
        await waitFor(() => gates.length >= expected, `${expected} chunk requests dispatched`);
        // Both jobs get a chance to dispatch before the cap is measured.
        await turn(5);
        expect(outstandingCreates).toBeLessThanOrEqual(5);
        const pending = gates.splice(0);
        pending.forEach((gate) => gate.resolve(verboseJson({ text: 'ok' })));
        released += pending.length;
        await turn();
      }

      await Promise.all(jobs);
      expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(12);
      // Exactly five, never ten: the cap comes from the shared limiter, not the pool size.
      expect(peakOutstandingCreates).toBe(5);
    });

    it('merges out-of-order completions in timeline order', async () => {
      preparedDurationSeconds = 90; // cores 0-30, 30-60, 60-90
      const gates = new Map<number, Deferred<VerboseJson>>();
      respond = (index) => {
        const gate = deferred<VerboseJson>();
        gates.set(index, gate);
        return gate.promise;
      };

      // Chunk-local word timings that each land inside their own core once rebased.
      const words: Record<number, TranscriptWord[]> = {
        0: [{ word: 'alpha', start: 1, end: 2 }],
        1: [{ word: 'bravo', start: 12.5, end: 13.5 }],
        2: [{ word: 'charlie', start: 12.5, end: 13.5 }],
      };

      const run = makeService({ maxConcurrentRequests: 5 }).transcribeAudio(FILE_URL);
      await waitFor(() => gates.size === 3, 'all three chunk requests dispatched');

      // Deliberately backwards: 2, then 0, then 1.
      for (const index of [2, 0, 1]) {
        gates.get(index)!.resolve(verboseJson({ text: `text ${index}`, words: words[index] }));
        await turn();
      }

      const result = await run;
      expect(result.text).toBe('alpha bravo charlie');
      expect(result.chunkCount).toBe(3);
      expect(loggedEvent('whisper.transcription.merged')).toMatchObject({ strategy: 'words' });
    });
  });

  describe('retry ladder', () => {
    it('honours a short Retry-After, applies a shared cooldown, and parks other workers', async () => {
      preparedDurationSeconds = 90; // 3 chunks
      sleepGate = [];
      const gate = sleepGate;
      const chunkOne = deferred<VerboseJson>();

      respond = (index, attempt) => {
        if (index === 0 && attempt === 1) return Promise.reject(groqApiError(429, '1'));
        if (index === 1) return chunkOne.promise;
        return Promise.resolve(verboseJson({ text: `chunk ${index}` }));
      };

      const run = makeService({ maxConcurrentRequests: 2 }).transcribeAudio(FILE_URL);

      // Chunks 0 and 1 go out; chunk 0 is rejected and its worker enters backoff.
      await waitFor(() => gate.length === 1, 'chunk 0 backoff started');
      expect(retrySleep).toHaveBeenCalledWith(1_000);
      expect(loggerMock.warn).toHaveBeenCalledWith(
        'whisper.api.cooldown_applied',
        expect.objectContaining({ chunkIndex: 0, retryAfterSeconds: 1, cooldownMs: 1_000 }),
      );

      // Free the second worker: it must park behind the shared cooldown instead of
      // dispatching chunk 2.
      chunkOne.resolve(verboseJson({ text: 'chunk 1' }));
      await waitFor(() => gate.length === 2, 'second worker parked on the shared cooldown');
      // Chunk 2 was never dispatched: its worker is sitting behind the shared cooldown.
      // (Order between 0 and 1 is not asserted — the two workers race on readFile.)
      expect([...createdChunkIndexes].sort()).toEqual([0, 1]);
      expect(outstandingCreates).toBe(0);

      gate.splice(0).forEach((entry) => entry.release());

      const result = await run;
      expect([...createdChunkIndexes].sort()).toEqual([0, 0, 1, 2]);
      expect(attemptsByChunk.get(0)).toBe(2);
      expect(attemptsByChunk.get(1)).toBe(1);
      expect(attemptsByChunk.get(2)).toBe(1);
      expect(result.chunkCount).toBe(3);
    });

    it('clamps a Retry-After above the cap to one 60s wait and retries', async () => {
      preparedDurationSeconds = 60; // two chunks -> the 360s long-form budget absorbs 60s
      respond = (index, attempt) => {
        if (index === 0 && attempt === 1) return Promise.reject(groqApiError(429, '90'));
        return Promise.resolve(twoChunkScript(index));
      };

      const result = await makeService({ maxChunkAttempts: 3 }).transcribeAudio(FILE_URL);

      expect(retrySleep).toHaveBeenCalledWith(60_000);
      expect(attemptsByChunk.get(0)).toBe(2);
      expect(result.text).toBe('first half second half');
      expect(loggerMock.warn).toHaveBeenCalledWith(
        'whisper.api.cooldown_applied',
        // The shared cooldown is the clamped wait, not the requested 90s.
        expect.objectContaining({ chunkIndex: 0, retryAfterSeconds: 90, cooldownMs: 60_000 }),
      );
    });

    it('honours a Retry-After below the cap verbatim rather than the cap', async () => {
      preparedDurationSeconds = 60;
      respond = (index, attempt) => {
        if (index === 0 && attempt === 1) return Promise.reject(groqApiError(429, '30'));
        return Promise.resolve(twoChunkScript(index));
      };

      const result = await makeService({ maxChunkAttempts: 3 }).transcribeAudio(FILE_URL);

      expect(retrySleep).toHaveBeenCalledWith(30_000);
      expect(retrySleep).not.toHaveBeenCalledWith(60_000);
      expect(attemptsByChunk.get(0)).toBe(2);
      expect(result.chunkCount).toBe(2);
    });

    // The backstop: clamping the wait must not let a job sleep past its own deadline.
    it('fails fast when the clamped Retry-After wait exceeds the remaining budget', async () => {
      preparedDurationSeconds = 10; // one chunk -> the 20s short-form budget cannot hold 60s
      respond = () => Promise.reject(groqApiError(429, '120'));

      const failure = (await makeService()
        .transcribeAudio(FILE_URL)
        .catch((error: unknown) => error)) as GroqTranscriptionError;

      expect(failure).toBeInstanceOf(GroqTranscriptionError);
      expect(failure.category).toBe('rate_limit');
      expect(failure.status).toBe(429);
      expect(failure.retryAfterSeconds).toBe(120);
      expect(failure.attempts).toBe(1);
      expect(mockTranscriptionsCreate).toHaveBeenCalledTimes(1);
      expect(retrySleep).not.toHaveBeenCalled();
    });

    it('recovers a retryable chunk within the three long-form attempts', async () => {
      preparedDurationSeconds = 60; // 2 chunks -> maxChunkAttempts applies
      respond = (index, attempt) => {
        if (index === 1 && attempt <= 2) return Promise.reject(groqApiError(500));
        return Promise.resolve(twoChunkScript(index));
      };

      const result = await makeService({ maxChunkAttempts: 3 }).transcribeAudio(FILE_URL);

      expect(attemptsByChunk.get(0)).toBe(1);
      expect(attemptsByChunk.get(1)).toBe(3);
      expect(result.chunkCount).toBe(2);
      expect(result.text).toBe('first half second half');
      // Jittered backoff, retryRandom pinned to 1: 250ms then 500ms.
      expect(retrySleep.mock.calls.map(([ms]) => ms)).toEqual([250, 500]);
    });

    it('rejects without a partial transcript once a chunk exhausts its attempts', async () => {
      preparedDurationSeconds = 60;
      respond = (index) => {
        if (index === 1) return Promise.reject(groqApiError(500));
        return Promise.resolve(verboseJson({ text: 'chunk 0' }));
      };

      const failure = (await makeService({ maxChunkAttempts: 3 })
        .transcribeAudio(FILE_URL)
        .catch((error: unknown) => error)) as GroqTranscriptionError;

      expect(failure).toBeInstanceOf(GroqTranscriptionError);
      expect(failure.category).toBe('server');
      expect(failure.attempts).toBe(3);
      expect(attemptsByChunk.get(1)).toBe(3);
      // No merge log means no transcript was ever assembled.
      expect(loggedEvent('whisper.transcription.merged')).toBeUndefined();
      expect(loggedEvent('whisper.transcription.completed')).toBeUndefined();
      expect(loggedEvent('whisper.transcription.aborted')).toMatchObject({
        chunkCount: 2,
        errorCategory: 'server',
      });
      expect(existsSync(await workDir())).toBe(false);
    });

    it('does not retry a permanent failure', async () => {
      preparedDurationSeconds = 60;
      respond = (index) => {
        if (index === 1) return Promise.reject(groqApiError(401));
        return Promise.resolve(verboseJson({ text: 'chunk 0' }));
      };

      const failure = (await makeService()
        .transcribeAudio(FILE_URL)
        .catch((error: unknown) => error)) as GroqTranscriptionError;

      expect(failure.category).toBe('authentication');
      expect(failure.retryable).toBe(false);
      expect(attemptsByChunk.get(1)).toBe(1);
      expect(retrySleep).not.toHaveBeenCalled();
    });

    it('stops scheduling new chunks after the first permanent failure', async () => {
      preparedDurationSeconds = 300; // 10 chunks
      const chunkOne = deferred<VerboseJson>();
      respond = (index) => {
        if (index === 0) return Promise.reject(groqApiError(403));
        if (index === 1) return chunkOne.promise;
        return Promise.resolve(verboseJson({ text: `chunk ${index}` }));
      };

      const run = makeService({ maxConcurrentRequests: 2 }).transcribeAudio(FILE_URL);
      const failure = run.catch((error: unknown) => error);

      await waitFor(
        () => attemptsByChunk.has(0) && attemptsByChunk.has(1),
        'first pair dispatched',
      );
      await turn(5); // let chunk 0's rejection register as the run failure
      chunkOne.resolve(verboseJson({ text: 'chunk 1' }));

      expect(((await failure) as GroqTranscriptionError).category).toBe('permission');

      const totalCreates = mockTranscriptionsCreate.mock.calls.length;
      expect(totalCreates).toBeLessThan(10);
      // Bounded by the two in-flight requests, so no chunk beyond index 1 was scheduled.
      expect(totalCreates).toBeLessThanOrEqual(3);
      expect(createdChunkIndexes.every((index) => index <= 1)).toBe(true);
    });

    it('rejects an oversized prepared chunk before any Groq request', async () => {
      preparedDurationSeconds = 10;
      chunkSizeBytesOverride = 30 * 1024 * 1024;

      await expect(
        makeService({ maxFileSizeBytes: 25 * 1024 * 1024 }).transcribeAudio(FILE_URL),
      ).rejects.toThrow(/Transcription failed: File size \(30MB\) exceeds maximum allowed size/);
      expect(mockTranscriptionsCreate).not.toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // Result, quality, logging
  // -------------------------------------------------------------------------

  describe('result, quality and logging', () => {
    // 60s -> cores 0-30 / 30-60, uploads 0-32.5 / 27.5-60.
    function segmentScript(index: number): VerboseJson {
      if (index === 0) {
        return {
          text: 'first half',
          segments: [
            {
              id: 0,
              start: 1,
              end: 2,
              text: 'first half',
              avg_logprob: -0.9,
              no_speech_prob: 0.02,
              compression_ratio: 1.3,
            },
          ],
        };
      }
      return {
        text: 'second half',
        segments: [
          // Rebased to 28-29: inside chunk 1's upload window but owned by chunk 0's core,
          // so the merge drops it and its terrible logprob must not reach the quality pass.
          {
            id: 0,
            start: 0.5,
            end: 1.5,
            text: 'overlap echo',
            avg_logprob: -4,
            no_speech_prob: 0.99,
            compression_ratio: 5,
          },
          {
            id: 1,
            start: 12.5,
            end: 13.5,
            text: 'second half',
            avg_logprob: -0.1,
            no_speech_prob: 0.03,
            compression_ratio: 1.1,
          },
        ],
      };
    }

    it('evaluates quality over the merged segments only', async () => {
      preparedDurationSeconds = 60;
      respond = async (index) => segmentScript(index);

      const result = await makeService().transcribeAudio(FILE_URL, 7, { requestId: 'tg_quality' });

      expect(loggedEvent('whisper.transcription.merged')).toMatchObject({ strategy: 'segments' });
      expect(result.quality).toBeDefined();
      expect(result.quality?.totalSegments).toBe(2);
      expect(result.quality?.flaggedSegments).toBeGreaterThanOrEqual(1);
      expect(result.quality?.flags.map((flag) => flag.reason)).toEqual(['low_avg_logprob']);
      // The discarded overlap segment's -4 never made it into the worst values.
      expect(result.quality?.worstValues.minAvgLogprob).toBe(-0.9);
      expect(loggerMock.warn).toHaveBeenCalledWith(
        'whisper.transcription.quality_flagged',
        expect.objectContaining({ requestId: 'tg_quality', flaggedSegments: 1 }),
      );
    });

    it('omits quality when monitoring is disabled', async () => {
      preparedDurationSeconds = 60;
      respond = async (index) => segmentScript(index);

      const result = await makeService({ qualityMonitoringEnabled: false }).transcribeAudio(
        FILE_URL,
      );

      expect(result.quality).toBeUndefined();
      expect(loggerMock.warn).not.toHaveBeenCalledWith(
        'whisper.transcription.quality_flagged',
        expect.anything(),
      );
    });

    it('logs completion metadata with a truncated transcript preview', async () => {
      preparedDurationSeconds = 60;
      const longText = 'Remember to review the quarterly roadmap with the platform team. '.repeat(
        4,
      );
      respond = async (index) =>
        verboseJson({
          text: index === 0 ? longText : '',
          segments: [{ id: 0, start: 0.5, end: 1.5, text: longText }],
        });

      const result = await makeService().transcribeAudio(FILE_URL);
      expect(result.text.length).toBeGreaterThan(100);

      const completed = loggedEvent('whisper.transcription.completed');
      expect(completed).toMatchObject({
        chunkCount: 2,
        durationSeconds: 60,
        mergeStrategy: 'segments',
        textLength: result.text.length,
      });

      const preview = completed?.textPreview as string;
      expect(preview.length).toBeLessThan(result.text.length);
      expect(preview).not.toBe(result.text);
    });

    it('never logs a long file URL verbatim', async () => {
      const longUrl = `https://api.telegram.org/file/bot${'9'.repeat(90)}/voice/file.ogg`;
      expect(longUrl.length).toBeGreaterThan(100);
      preparedDurationSeconds = 10;

      await makeService().transcribeAudio(longUrl);

      const serialized = JSON.stringify(logPayloads());
      expect(serialized).not.toContain(longUrl);
      expect(serialized).toContain('...[truncated]...');
    });

    it('removes the work directory on the success path', async () => {
      preparedDurationSeconds = 60;

      await makeService().transcribeAudio(FILE_URL);

      const dir = await workDir();
      expect(dir).toContain('jarvis-audio-');
      expect(existsSync(dir)).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Config validation
  // -------------------------------------------------------------------------

  describe('config validation', () => {
    it('requires an API key', () => {
      expect(() => new WhisperService()).toThrow(/Groq API key is required/);
    });

    it('accepts the API key from the environment', () => {
      process.env.GROQ_API_KEY = 'from-env';
      expect(() => new WhisperService()).not.toThrow();
    });

    it.each([0, 2.5])('rejects maxConcurrentRequests %p', (maxConcurrentRequests) => {
      expect(() => makeService({ maxConcurrentRequests })).toThrow(
        /GROQ_TRANSCRIPTION_MAX_CONCURRENCY must be a positive/,
      );
    });

    it('rejects a negative overlapSeconds', () => {
      expect(() => makeService({ overlapSeconds: -1 })).toThrow(
        'Whisper overlapSeconds must be a non-negative finite number.',
      );
    });

    it('accepts a zero overlap', () => {
      expect(makeService({ overlapSeconds: 0 }).getConfig()).toMatchObject({ overlapSeconds: 0 });
    });

    // A service built without options (agent CLI, tests) must land on the same numbers the
    // ladder asserts — one owner for the two audio budgets, not three.
    it('resolves the audio budgets through the turn-timeout ladder resolvers', () => {
      process.env.GROQ_AUDIO_PREPARE_TIMEOUT_MS = '90000';
      process.env.GROQ_AUDIO_TRANSCRIPTION_TIMEOUT_MS = '300000';

      expect(new WhisperService({ apiKey: 'groq-test-key' }).getConfig()).toMatchObject({
        prepareTimeoutMs: resolveAudioPrepareTimeoutMs(),
        longFormTimeoutMs: resolveAudioTranscriptionTimeoutMs(),
      });
      expect(resolveAudioPrepareTimeoutMs()).toBe(90_000);
      expect(resolveAudioTranscriptionTimeoutMs()).toBe(300_000);
    });

    it.each(['0', 'abc'])('rejects GROQ_AUDIO_PREPARE_TIMEOUT_MS=%p', (value) => {
      process.env.GROQ_AUDIO_PREPARE_TIMEOUT_MS = value;
      expect(() => new WhisperService({ apiKey: 'groq-test-key' })).toThrow(
        'GROQ_AUDIO_PREPARE_TIMEOUT_MS must be finite and greater than zero',
      );
    });
  });
});
