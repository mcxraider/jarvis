// tests/unit/utils/ai/audioConverter.test.ts
//
// `child_process.spawn` and a handful of `fs/promises` calls are wrapped in jest.fn()
// around their real implementations. Almost every test replaces the spawn impl with a
// scripted fake for determinism; the final describe restores the real ones so one
// integration test can drive the bundled FFmpeg binary for real.

import { EventEmitter } from 'events';
import { basename, join } from 'path';
import { tmpdir } from 'os';

jest.mock('child_process', () => {
  const actual = jest.requireActual('child_process');
  return { ...actual, spawn: jest.fn(actual.spawn) };
});

jest.mock('fs/promises', () => {
  const actual = jest.requireActual('fs/promises');
  return {
    ...actual,
    stat: jest.fn(actual.stat),
    rm: jest.fn(actual.rm),
    rmdir: jest.fn(actual.rmdir),
    unlink: jest.fn(actual.unlink),
    mkdir: jest.fn(actual.mkdir),
  };
});

import * as ffmpegInstaller from '@ffmpeg-installer/ffmpeg';
import { spawn } from 'child_process';
import { stat, rm, rmdir, unlink, mkdir } from 'fs/promises';

import { AudioConverter } from '../../../../src/utils/ai/audioConverter';
import { AudioAdmissionError } from '../../../../src/utils/ai/audio-admission-error';
import { planAudioChunks } from '../../../../src/utils/ai/audio-chunk-plan';
import { AUDIO_LIMITS } from '../../../../src/utils/ai/audio-limits';

const realCp = jest.requireActual<typeof import('child_process')>('child_process');
const realFs = jest.requireActual<typeof import('fs/promises')>('fs/promises');

const mockSpawn = spawn as jest.MockedFunction<typeof spawn>;
const mockStat = stat as jest.MockedFunction<typeof stat>;
const mockRm = rm as jest.MockedFunction<typeof rm>;
const mockRmdir = rmdir as jest.MockedFunction<typeof rmdir>;
const mockUnlink = unlink as jest.MockedFunction<typeof unlink>;
const mockMkdir = mkdir as jest.MockedFunction<typeof mkdir>;

const WORK_DIR = '/tmp/jarvis-audio-test';
const INPUT_PATH = '/tmp/jarvis-audio-test/source.oga';
const NORMALIZED = join(WORK_DIR, 'normalized.flac');

// ---------------------------------------------------------------------------
// scripted spawn harness
// ---------------------------------------------------------------------------

interface MockProcess extends EventEmitter {
  stdout: EventEmitter;
  stderr: EventEmitter;
  kill: jest.Mock;
}

interface SpawnCall {
  bin: string;
  args: string[];
  proc: MockProcess;
}

interface ScriptStep {
  progress?: string[];
  stderr?: string;
  code?: number | null;
  error?: Error;
  /** Emit nothing at all — used to exercise the timeout path. */
  hang?: boolean;
}

let calls: SpawnCall[];
let timeline: string[];
let scripts: ScriptStep[];
let defaultScript: ScriptStep;
let statSizes: Map<string, number>;
let defaultStatSize: number | undefined;

function createMockProcess(): MockProcess {
  const proc = new EventEmitter() as MockProcess;
  proc.stdout = new EventEmitter();
  proc.stderr = new EventEmitter();
  proc.kill = jest.fn();
  return proc;
}

function runScript(step: ScriptStep, proc: MockProcess, index: number): void {
  if (step.hang) return;
  if (step.progress?.length) {
    proc.stdout.emit('data', Buffer.from(`${step.progress.join('\n')}\n`));
  }
  if (step.stderr) {
    proc.stderr.emit('data', Buffer.from(step.stderr));
  }
  if (step.error) {
    timeline.push(`error:${index}`);
    proc.emit('error', step.error);
    return;
  }
  timeline.push(`close:${index}`);
  proc.emit('close', step.code ?? 0);
}

function installScriptedSpawn(): void {
  mockSpawn.mockImplementation(((bin: string, args: string[]) => {
    const index = calls.length;
    const proc = createMockProcess();
    calls.push({ bin, args, proc });
    timeline.push(`spawn:${index}`);
    const step = scripts[index] ?? defaultScript;
    void Promise.resolve().then(() => runScript(step, proc, index));
    return proc;
  }) as unknown as typeof spawn);
}

function installScriptedStat(): void {
  mockStat.mockImplementation((async (target: string) => {
    const key = basename(String(target));
    const size = statSizes.has(key) ? statSizes.get(key) : defaultStatSize;
    if (size === undefined) {
      throw Object.assign(new Error(`ENOENT: ${key}`), { code: 'ENOENT' });
    }
    return { size } as unknown as Awaited<ReturnType<typeof stat>>;
  }) as unknown as typeof stat);
}

/** Progress lines FFmpeg would emit for a file of the given decoded length. */
function progressForSeconds(seconds: number): string[] {
  return [
    'bitrate=  64.0kbits/s',
    `out_time_us=${Math.round(seconds * 1_000_000)}`,
    'progress=continue',
    'progress=end',
  ];
}

function argsOf(index: number): string[] {
  return calls[index].args;
}

function hasPair(args: string[], flag: string, value: string): boolean {
  return args.some((arg, i) => arg === flag && args[i + 1] === value);
}

// ---------------------------------------------------------------------------

describe('AudioConverter', () => {
  beforeEach(() => {
    calls = [];
    timeline = [];
    scripts = [];
    defaultScript = { code: 0 };
    statSizes = new Map();
    defaultStatSize = 4096;
    installScriptedSpawn();
    installScriptedStat();
  });

  describe('isFFmpegAvailable()', () => {
    it('returns true when ffmpeg exits with code 0', async () => {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc as never);

      const promise = AudioConverter.isFFmpegAvailable();
      proc.emit('close', 0);

      await expect(promise).resolves.toBe(true);
      expect(mockSpawn).toHaveBeenCalledWith(ffmpegInstaller.path, ['-version']);
    });

    it('returns false when ffmpeg exits non-zero', async () => {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc as never);

      const promise = AudioConverter.isFFmpegAvailable();
      proc.emit('close', 1);

      await expect(promise).resolves.toBe(false);
    });

    it('returns false when spawn emits an error event', async () => {
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc as never);

      const promise = AudioConverter.isFFmpegAvailable();
      proc.emit('error', new Error('spawn ENOENT'));

      await expect(promise).resolves.toBe(false);
    });

    it('returns false and kills the child after 5s', async () => {
      jest.useFakeTimers();
      const proc = createMockProcess();
      mockSpawn.mockReturnValue(proc as never);

      const promise = AudioConverter.isFFmpegAvailable();
      await jest.advanceTimersByTimeAsync(5000);

      await expect(promise).resolves.toBe(false);
      expect(proc.kill).toHaveBeenCalled();
      jest.useRealTimers();
    });
  });

  describe('prepare() normalization command', () => {
    it('spawns the bundled installer binary with every encoding invariant', async () => {
      scripts = [{ progress: progressForSeconds(20) }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(calls).toHaveLength(1);
      expect(calls[0].bin).toBe(ffmpegInstaller.path);

      const args = argsOf(0);
      expect(args).toContain('-nostdin');
      expect(args).toContain('-hide_banner');
      expect(args).toContain('-nostats');
      expect(args).toContain('-vn');
      expect(args).toContain('-y');
      expect(hasPair(args, '-progress', 'pipe:1')).toBe(true);
      expect(hasPair(args, '-map', '0:a:0')).toBe(true);
      expect(hasPair(args, '-ar', '16000')).toBe(true);
      expect(hasPair(args, '-ac', '1')).toBe(true);
      expect(hasPair(args, '-c:a', 'flac')).toBe(true);
      expect(hasPair(args, '-i', INPUT_PATH)).toBe(true);

      // Output is the last argument, a .flac, inside the caller's work directory.
      const output = args[args.length - 1];
      expect(output).toBe(NORMALIZED);
      expect(output.endsWith('.flac')).toBe(true);
      expect(output.startsWith(`${WORK_DIR}/`)).toBe(true);
      expect(result.normalizedPath).toBe(NORMALIZED);

      // Normalization reads the whole file: no input seeking.
      expect(args).not.toContain('-ss');
      expect(args).not.toContain('-t');
    });

    it('still normalizes a bare .flac input instead of passing it through', async () => {
      scripts = [{ progress: progressForSeconds(12) }];
      const flacInput = join(WORK_DIR, 'source.flac');

      const result = await AudioConverter.prepare({ inputPath: flacInput, workDir: WORK_DIR });

      expect(calls).toHaveLength(1);
      expect(hasPair(argsOf(0), '-i', flacInput)).toBe(true);
      expect(hasPair(argsOf(0), '-ar', '16000')).toBe(true);
      expect(result.normalizedPath).toBe(NORMALIZED);
      expect(result.normalizedPath).not.toBe(flacInput);
    });
  });

  describe('prepare() duration measurement', () => {
    it('derives duration from out_time_us progress lines on stdout', async () => {
      scripts = [{ progress: ['out_time_us=1000000', 'out_time_us=20000000', 'progress=end'] }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(result.durationSeconds).toBe(20);
    });

    it('treats out_time_ms as microseconds', async () => {
      scripts = [{ progress: ['out_time_ms=25000000', 'progress=end'] }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(result.durationSeconds).toBe(25);
    });

    it('falls back to the textual out_time clock', async () => {
      scripts = [{ progress: ['out_time=00:01:30.500000', 'progress=end'] }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(result.durationSeconds).toBe(90.5);
    });

    it('parses a partial trailing progress line flushed at close', async () => {
      // No trailing newline: the parser must flush the buffered line on close.
      mockSpawn.mockImplementation(((bin: string, args: string[]) => {
        const proc = createMockProcess();
        calls.push({ bin, args, proc });
        void Promise.resolve().then(() => {
          proc.stdout.emit('data', Buffer.from('out_time_us=180'));
          proc.stdout.emit('data', Buffer.from('00000'));
          proc.emit('close', 0);
        });
        return proc;
      }) as unknown as typeof spawn);

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(result.durationSeconds).toBe(18);
    });

    it('falls back to the stderr Duration line when progress is silent', async () => {
      scripts = [{ stderr: '  Duration: 00:00:12.34, start: 0.000000, bitrate: 64 kb/s\n' }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(result.durationSeconds).toBe(12.34);
    });

    it('rejects when no duration can be determined anywhere', async () => {
      scripts = [{ stderr: 'some unrelated ffmpeg chatter\n' }];

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow('Audio preparation failed: could not determine audio duration');
      expect(calls).toHaveLength(1);
    });
  });

  describe('prepare() duration limit', () => {
    it('kills ffmpeg mid-encode as soon as the limit is crossed', async () => {
      // Drive progress manually so nothing ever closes the process: only the
      // duration guard can settle this call.
      mockSpawn.mockImplementation(((bin: string, args: string[]) => {
        const proc = createMockProcess();
        calls.push({ bin, args, proc });
        void Promise.resolve().then(() => {
          proc.stdout.emit('data', Buffer.from('out_time_us=5000000\n'));
          proc.stdout.emit('data', Buffer.from('out_time_us=11000000\n'));
        });
        return proc;
      }) as unknown as typeof spawn);

      const error = await AudioConverter.prepare({
        inputPath: INPUT_PATH,
        workDir: WORK_DIR,
        maxDurationSeconds: 10,
      }).catch((e: unknown) => e);

      expect(error).toBeInstanceOf(AudioAdmissionError);
      expect((error as AudioAdmissionError).reason).toBe('too_long');
      expect((error as AudioAdmissionError).observed).toBe(11);
      expect((error as AudioAdmissionError).limit).toBe(10);
      expect(calls[0].proc.kill).toHaveBeenCalledWith('SIGKILL');
      // Killed before encoding finished: no chunk extraction may have started.
      expect(calls).toHaveLength(1);
    });

    it('rejects an over-limit duration discovered only after a clean exit', async () => {
      // Progress silent, so the mid-stream guard never fires; stderr reveals 20s.
      scripts = [{ stderr: '  Duration: 00:00:20.00, bitrate: 64 kb/s\n' }];

      const error = await AudioConverter.prepare({
        inputPath: INPUT_PATH,
        workDir: WORK_DIR,
        maxDurationSeconds: 10,
      }).catch((e: unknown) => e);

      expect(error).toBeInstanceOf(AudioAdmissionError);
      expect((error as AudioAdmissionError).reason).toBe('too_long');
      expect((error as AudioAdmissionError).observed).toBe(20);
      expect(calls).toHaveLength(1);
    });

    it('accepts audio of exactly maxDurationSeconds', async () => {
      scripts = [{ progress: progressForSeconds(30) }];

      const result = await AudioConverter.prepare({
        inputPath: INPUT_PATH,
        workDir: WORK_DIR,
        maxDurationSeconds: 30,
      });

      expect(result.durationSeconds).toBe(30);
      expect(result.chunks).toHaveLength(1);
    });

    it('accepts exactly the production 20-minute ceiling', async () => {
      scripts = [{ progress: progressForSeconds(AUDIO_LIMITS.MAX_DURATION_SECONDS) }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(result.durationSeconds).toBe(1200);
      expect(result.chunks).toHaveLength(40);
    });
  });

  describe('prepare() chunking', () => {
    it('reuses the normalized file as the single chunk when no split is needed', async () => {
      scripts = [{ progress: progressForSeconds(20) }];
      statSizes.set('normalized.flac', 7777);

      const result = await AudioConverter.prepare({
        inputPath: INPUT_PATH,
        workDir: WORK_DIR,
        coreSeconds: 30,
      });

      expect(calls).toHaveLength(1);
      expect(result.chunks).toHaveLength(1);
      expect(result.chunks[0].path).toBe(result.normalizedPath);
      expect(result.chunks[0].sizeBytes).toBe(7777);
      expect(result.normalizedSizeBytes).toBe(7777);
      expect(result.chunks[0]).toMatchObject({
        index: 0,
        startSeconds: 0,
        endSeconds: 20,
        coreStartSeconds: 0,
        coreEndSeconds: 20,
      });
      expect(typeof result.prepareTimeMs).toBe('number');
    });

    it('extracts two chunks sequentially with fast input seeking for 60s audio', async () => {
      scripts = [{ progress: progressForSeconds(60) }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      const plan = planAudioChunks(60);
      expect(plan.map((p) => [p.startSeconds, p.endSeconds])).toEqual([
        [0, 32.5],
        [27.5, 60],
      ]);

      expect(calls).toHaveLength(3);

      const first = argsOf(1);
      expect(hasPair(first, '-ss', '0')).toBe(true);
      expect(hasPair(first, '-t', '32.5')).toBe(true);
      expect(hasPair(first, '-i', NORMALIZED)).toBe(true);
      expect(first.indexOf('-ss')).toBeLessThan(first.indexOf('-i'));
      expect(first.indexOf('-t')).toBeLessThan(first.indexOf('-i'));
      expect(first[first.length - 1]).toBe(join(WORK_DIR, 'chunk-000.flac'));

      const second = argsOf(2);
      expect(hasPair(second, '-ss', '27.5')).toBe(true);
      expect(hasPair(second, '-t', '32.5')).toBe(true);
      expect(second.indexOf('-ss')).toBeLessThan(second.indexOf('-i'));
      expect(second[second.length - 1]).toBe(join(WORK_DIR, 'chunk-001.flac'));

      // Chunk N is only spawned after chunk N-1's child has closed.
      expect(timeline).toEqual(['spawn:0', 'close:0', 'spawn:1', 'close:1', 'spawn:2', 'close:2']);

      expect(result.chunks.map((c) => c.index)).toEqual([0, 1]);
      expect(result.chunks.map((c) => c.path)).toEqual([
        join(WORK_DIR, 'chunk-000.flac'),
        join(WORK_DIR, 'chunk-001.flac'),
      ]);
    });

    it('keeps the encoding invariants on every chunk extraction', async () => {
      scripts = [{ progress: progressForSeconds(60) }];

      await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      for (const index of [1, 2]) {
        const args = argsOf(index);
        expect(args).toContain('-nostdin');
        expect(args).toContain('-vn');
        expect(hasPair(args, '-map', '0:a:0')).toBe(true);
        expect(hasPair(args, '-ar', '16000')).toBe(true);
        expect(hasPair(args, '-ac', '1')).toBe(true);
        expect(hasPair(args, '-c:a', 'flac')).toBe(true);
      }
    });

    it('produces 40 zero-padded chunks in order for 20-minute audio', async () => {
      scripts = [{ progress: progressForSeconds(1200) }];

      const result = await AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR });

      expect(calls).toHaveLength(41);
      expect(result.chunks).toHaveLength(40);
      expect(result.chunks.map((c) => c.index)).toEqual(Array.from({ length: 40 }, (_, i) => i));
      expect(result.chunks.map((c) => basename(c.path))).toEqual(
        Array.from({ length: 40 }, (_, i) => `chunk-${String(i).padStart(3, '0')}.flac`),
      );
      expect(basename(result.chunks[0].path)).toBe('chunk-000.flac');
      expect(basename(result.chunks[39].path)).toBe('chunk-039.flac');
      expect(result.chunks[39].coreEndSeconds).toBe(1200);
      // Strict ordering: every spawn is preceded by the previous child's close.
      expect(timeline).toHaveLength(82);
      expect(timeline.filter((_, i) => i % 2 === 0)).toEqual(
        Array.from({ length: 41 }, (_, i) => `spawn:${i}`),
      );
    });

    it('rejects when a chunk exceeds the transcription attachment limit', async () => {
      scripts = [{ progress: progressForSeconds(60) }];
      statSizes.set('chunk-000.flac', 5000);

      await expect(
        AudioConverter.prepare({
          inputPath: INPUT_PATH,
          workDir: WORK_DIR,
          maxChunkBytes: 4096,
        }),
      ).rejects.toThrow(
        'Audio preparation failed: prepared chunk exceeds the transcription attachment limit',
      );

      // Failed on chunk 0, so chunk 1 was never spawned.
      expect(calls).toHaveLength(2);
    });

    it('rejects a zero-byte chunk', async () => {
      scripts = [{ progress: progressForSeconds(60) }];
      statSizes.set('chunk-000.flac', 0);

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow('Audio preparation failed: prepared chunk 0 is empty');
      expect(calls).toHaveLength(2);
    });

    it('rejects the whole call when a chunk spawn exits non-zero', async () => {
      scripts = [
        { progress: progressForSeconds(60) },
        { code: 1, stderr: 'Invalid data found when processing input\n' },
      ];

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow('Audio preparation failed: Invalid data found when processing input');
      expect(calls).toHaveLength(2);
    });
  });

  describe('prepare() failure modes', () => {
    it('surfaces the ffmpeg error line for corrupt input', async () => {
      scripts = [
        {
          code: 1,
          stderr: 'source.oga: Invalid data found when processing input\n',
        },
      ];

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow(
        'Audio preparation failed: source.oga: Invalid data found when processing input',
      );
    });

    it('reports a missing audio stream distinguishably', async () => {
      scripts = [
        {
          code: 1,
          stderr: "Stream map '0:a:0' matches no streams.\nTo ignore this, add -ignore_unknown\n",
        },
      ];

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow('Audio preparation failed: no audio stream found');
    });

    it('reports an unavailable binary with the installer hint on ENOENT', async () => {
      scripts = [{ error: new Error('spawn /nope/ffmpeg ENOENT') }];

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow(
        'FFmpeg is not available: FFmpeg executable not found. Ensure `@ffmpeg-installer/ffmpeg` is installed.',
      );
    });

    it('reports other spawn errors verbatim', async () => {
      scripts = [{ error: new Error('EACCES permission denied') }];

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow('FFmpeg is not available: EACCES permission denied');
    });

    it('kills the child and rejects when the whole-call budget expires', async () => {
      jest.useFakeTimers();
      scripts = [{ hang: true }];

      const promise = AudioConverter.prepare({
        inputPath: INPUT_PATH,
        workDir: WORK_DIR,
        timeoutMs: 120_000,
      }).catch((e: unknown) => e);

      await jest.advanceTimersByTimeAsync(119_000);
      expect(calls[0].proc.kill).not.toHaveBeenCalled();

      await jest.advanceTimersByTimeAsync(1_000);
      const error = await promise;

      expect((error as Error).message).toBe('Audio preparation timed out after 120s');
      expect(calls[0].proc.kill).toHaveBeenCalledWith('SIGKILL');
      // No chunk extraction may start after the kill.
      expect(calls).toHaveLength(1);
      jest.useRealTimers();
    });

    it('shares one budget across normalization and chunk extraction', async () => {
      jest.useFakeTimers();
      scripts = [{ progress: progressForSeconds(60) }, { hang: true }];

      const promise = AudioConverter.prepare({
        inputPath: INPUT_PATH,
        workDir: WORK_DIR,
        timeoutMs: 30_000,
      }).catch((e: unknown) => e);

      await jest.advanceTimersByTimeAsync(30_000);
      const error = await promise;

      expect((error as Error).message).toBe('Audio preparation timed out after 30s');
      expect(calls).toHaveLength(2);
      expect(calls[1].proc.kill).toHaveBeenCalledWith('SIGKILL');
      jest.useRealTimers();
    });

    it('rejects a zero-byte normalized output', async () => {
      scripts = [{ progress: progressForSeconds(20) }];
      statSizes.set('normalized.flac', 0);

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow('Audio preparation failed: FFmpeg produced no output');
      expect(calls).toHaveLength(1);
    });

    it('rejects a missing normalized output', async () => {
      scripts = [{ progress: progressForSeconds(20) }];
      defaultStatSize = undefined; // every stat throws ENOENT

      await expect(
        AudioConverter.prepare({ inputPath: INPUT_PATH, workDir: WORK_DIR }),
      ).rejects.toThrow('Audio preparation failed: FFmpeg produced no output');
    });
  });

  describe('prepare() work directory lifecycle', () => {
    it('never creates or removes the caller-owned work directory', async () => {
      const workDir = await realFs.mkdtemp(join(tmpdir(), 'jarvis-audio-lifecycle-'));
      try {
        scripts = [{ code: 1, stderr: 'Invalid data found when processing input\n' }];

        await expect(
          AudioConverter.prepare({ inputPath: join(workDir, 'source.oga'), workDir }),
        ).rejects.toThrow('Audio preparation failed');

        // Directory survives the failure — the caller's finally owns cleanup.
        await expect(realFs.stat(workDir)).resolves.toBeDefined();
        expect(mockRm).not.toHaveBeenCalled();
        expect(mockRmdir).not.toHaveBeenCalled();
        expect(mockUnlink).not.toHaveBeenCalled();
        expect(mockMkdir).not.toHaveBeenCalled();
      } finally {
        await realFs.rm(workDir, { recursive: true, force: true });
      }
    });
  });

  // -------------------------------------------------------------------------
  // Real FFmpeg. Command construction is not enough: this proves the output is
  // genuinely 16 kHz mono FLAC. Skips gracefully if the binary cannot run.
  // -------------------------------------------------------------------------
  describe('prepare() against the real bundled FFmpeg', () => {
    beforeEach(() => {
      mockSpawn.mockImplementation(realCp.spawn as unknown as typeof spawn);
      mockStat.mockImplementation(realFs.stat as unknown as typeof stat);
    });

    function probe(filePath: string): Promise<string> {
      return new Promise((resolve) => {
        const child = realCp.spawn(ffmpegInstaller.path, ['-hide_banner', '-i', filePath]);
        let stderr = '';
        child.stderr.on('data', (d) => {
          stderr += d.toString();
        });
        child.once('close', () => resolve(stderr));
      });
    }

    function synthesize(outputPath: string, seconds: number): Promise<number | null> {
      return new Promise((resolve, reject) => {
        const child = realCp.spawn(ffmpegInstaller.path, [
          '-nostdin',
          '-hide_banner',
          '-f',
          'lavfi',
          '-i',
          `sine=frequency=440:duration=${seconds}`,
          '-c:a',
          'pcm_s16le',
          '-y',
          outputPath,
        ]);
        child.once('error', reject);
        child.once('close', resolve);
      });
    }

    it('processes the real assets/audio-test.ogg file (27.6s → 1 chunk, no split)', async () => {
      const available = await AudioConverter.isFFmpegAvailable();
      expect(typeof available).toBe('boolean');
      if (!available) return;

      const assetPath = join(__dirname, '../../../../assets/audio-test.ogg');
      const workDir = await realFs.mkdtemp(join(tmpdir(), 'jarvis-audio-asset-'));
      try {
        const result = await AudioConverter.prepare({ inputPath: assetPath, workDir });

        console.log(`Duration: ${result.durationSeconds.toFixed(2)}s`);
        console.log(`Normalized size: ${result.normalizedSizeBytes} bytes`);
        console.log(`Chunks: ${result.chunks.length}`);
        for (const c of result.chunks) {
          console.log(
            `  Chunk ${c.index}: upload [${c.startSeconds}–${c.endSeconds}s], core [${c.coreStartSeconds}–${c.coreEndSeconds}s], ${c.sizeBytes} bytes`,
          );
        }

        expect(result.durationSeconds).toBeGreaterThan(25);
        expect(result.durationSeconds).toBeLessThan(30);
        expect(result.normalizedSizeBytes).toBeGreaterThan(0);
        // 27.6s < CORE_SECONDS (30s) → no split
        expect(result.chunks).toHaveLength(1);
        expect(result.chunks[0].startSeconds).toBe(0);
        expect(result.chunks[0].coreStartSeconds).toBe(0);

        const info = await probe(result.normalizedPath);
        expect(info).toMatch(/Audio:\s*flac/);
        expect(info).toMatch(/16000 Hz/);
        expect(info).toMatch(/\bmono\b/);
      } finally {
        await realFs.rm(workDir, { recursive: true, force: true });
      }
    }, 30_000);

    it('processes the real assets/MWINIWIO-30-Aug.m4a.mp4 file (3m50s → 8 chunks)', async () => {
      const available = await AudioConverter.isFFmpegAvailable();
      expect(typeof available).toBe('boolean');
      if (!available) return;

      const assetPath = join(__dirname, '../../../../assets/MWINIWIO-30-Aug.m4a.mp4');
      const workDir = await realFs.mkdtemp(join(tmpdir(), 'jarvis-audio-mwiniwio-'));
      try {
        const result = await AudioConverter.prepare({ inputPath: assetPath, workDir });

        console.log(`Duration: ${result.durationSeconds.toFixed(2)}s`);
        console.log(`Normalized size: ${(result.normalizedSizeBytes / 1024).toFixed(0)} KB`);
        console.log(`Chunks: ${result.chunks.length}`);
        for (const c of result.chunks) {
          console.log(
            `  Chunk ${c.index}: upload [${c.startSeconds}–${c.endSeconds}s], core [${c.coreStartSeconds}–${c.coreEndSeconds}s], ${(c.sizeBytes / 1024).toFixed(0)} KB`,
          );
        }

        expect(result.durationSeconds).toBeGreaterThan(229);
        expect(result.durationSeconds).toBeLessThan(232);
        expect(result.normalizedSizeBytes).toBeGreaterThan(0);
        // 230.5s / 30s core = ceil(7.68) = 8 chunks
        expect(result.chunks).toHaveLength(8);
        // First chunk starts at 0
        expect(result.chunks[0].startSeconds).toBe(0);
        // Last chunk ends at the file duration
        expect(result.chunks[7].coreEndSeconds).toBeCloseTo(result.durationSeconds, 1);
        // Internal chunks have an overlap window wider than their core
        expect(result.chunks[1].startSeconds).toBeLessThan(result.chunks[1].coreStartSeconds);

        const info = await probe(result.normalizedPath);
        expect(info).toMatch(/Audio:\s*flac/);
        expect(info).toMatch(/16000 Hz/);
        expect(info).toMatch(/\bmono\b/);
      } finally {
        await realFs.rm(workDir, { recursive: true, force: true });
      }
    }, 120_000);

    it('normalizes 35s of synthesized audio to 16 kHz mono FLAC and splits it in two', async () => {
      const available = await AudioConverter.isFFmpegAvailable();
      expect(typeof available).toBe('boolean');
      if (!available) return;

      const workDir = await realFs.mkdtemp(join(tmpdir(), 'jarvis-audio-real-'));
      try {
        const inputPath = join(workDir, 'source.wav');
        expect(await synthesize(inputPath, 35)).toBe(0);

        const result = await AudioConverter.prepare({ inputPath, workDir });

        expect(result.durationSeconds).toBeGreaterThan(34.5);
        expect(result.durationSeconds).toBeLessThan(35.5);
        expect(result.normalizedSizeBytes).toBeGreaterThan(0);
        expect(result.chunks).toHaveLength(2);
        expect(result.chunks[0].path).not.toBe(result.normalizedPath);
        for (const chunk of result.chunks) {
          expect(chunk.sizeBytes).toBeGreaterThan(0);
        }

        const normalizedInfo = await probe(result.normalizedPath);
        expect(normalizedInfo).toMatch(/Audio:\s*flac/);
        expect(normalizedInfo).toMatch(/16000 Hz/);
        expect(normalizedInfo).toMatch(/\bmono\b/);

        const chunkInfo = await probe(result.chunks[1].path);
        expect(chunkInfo).toMatch(/Audio:\s*flac/);
        expect(chunkInfo).toMatch(/16000 Hz/);
        expect(chunkInfo).toMatch(/\bmono\b/);
      } finally {
        await realFs.rm(workDir, { recursive: true, force: true });
      }
    }, 30_000);
  });
});
