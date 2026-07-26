jest.unmock('../../../src/utils/logger');

process.env.LOG_LEVEL = 'debug';

import fs from 'fs';
import os from 'os';
import path from 'path';

const originalLogDir = process.env.JARVIS_LOG_DIR;
const LOG_DIR = fs.mkdtempSync(path.join(os.tmpdir(), `jarvis-logger-${process.pid}-`));
process.env.JARVIS_LOG_DIR = LOG_DIR;

afterAll(() => {
  if (originalLogDir === undefined) {
    delete process.env.JARVIS_LOG_DIR;
  } else {
    process.env.JARVIS_LOG_DIR = originalLogDir;
  }
  fs.rmSync(LOG_DIR, { recursive: true, force: true });
});

function linesContaining(filename: string, marker: string): string[] {
  const filepath = path.join(LOG_DIR, filename);
  if (!fs.existsSync(filepath)) return [];
  return fs.readFileSync(filepath, 'utf8').trim().split('\n').filter((l) => l.includes(marker));
}

function jsonLinesContaining(filename: string, marker: string): Record<string, unknown>[] {
  return linesContaining(filename, marker)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean) as Record<string, unknown>[];
}

// Each describe uses its own fresh module to get isolated worker state.
// This avoids cross-contamination from shutdown/crash tests.

describe('async logger queue (original)', () => {
  const mod = jest.requireActual<typeof import('../../../src/utils/logger')>(
    '../../../src/utils/logger',
  );

  afterAll(async () => {
    await mod.shutdownLogger();
  });

  it('bounds a burst and preserves an error by evicting queued debug work', async () => {
    for (let index = 0; index < 500; index += 1) {
      mod.logger.debug('logger.queue.test', { index });
    }
    mod.logger.error('logger.queue.priority_test');

    const burstStats = mod.getLoggerStats();
    expect(burstStats.queue_depth).toBeLessThanOrEqual(500);
    expect(burstStats.events_accepted).toBe(501);
    expect(burstStats.events_dropped.debug).toBe(1);
    expect(burstStats.events_dropped.error).toBe(0);
    expect(burstStats.queue_high_water).toBe(500);

    await mod.flushLogger(10_000);
    const flushedStats = mod.getLoggerStats();
    expect(flushedStats.queue_depth).toBe(0);
    expect(flushedStats.flush_count).toBe(1);
    expect(flushedStats.last_write_time).toBeDefined();
    expect(flushedStats.worker_alive).toBe(true);
  }, 15_000);
});

describe('circular object handling', () => {
  let mod: typeof import('../../../src/utils/logger');
  const MARKER = `circular_test_${process.pid}`;

  beforeAll(() => {
    jest.resetModules();
    jest.unmock('../../../src/utils/logger');
    mod = require('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger();
  });

  it('logs objects with circular references without throwing', async () => {
    const circular: Record<string, unknown> = { name: MARKER };
    circular.self = circular;

    expect(() => mod.logger.error(MARKER, circular)).not.toThrow();
    await mod.flushLogger(8000);
    await new Promise((resolve) => setTimeout(resolve, 200));

    const lines = linesContaining('error.log', MARKER);
    expect(lines.length).toBeGreaterThan(0);
    expect(lines.some((l) => l.includes('[Circular]'))).toBe(true);
  }, 10_000);
});

describe('error normalization', () => {
  let mod: typeof import('../../../src/utils/logger');
  const MARKER = `error_chain_test_${process.pid}`;

  beforeAll(() => {
    jest.resetModules();
    jest.unmock('../../../src/utils/logger');
    mod = require('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger();
  });

  it('serializes Error with cause chain as structured {name, message, stack}', async () => {
    const cause = new TypeError('root cause');
    const outer = new Error('outer error');
    (outer as any).cause = cause;

    mod.logger.error(MARKER, { err: outer });
    await mod.flushLogger(8000);
    await new Promise((resolve) => setTimeout(resolve, 200));

    const entries = jsonLinesContaining('error.log', MARKER);
    expect(entries.length).toBeGreaterThan(0);
    const entry = entries[entries.length - 1];
    expect(entry.err).toMatchObject({
      name: 'Error',
      message: 'outer error',
    });
    expect((entry.err as any).stack).toContain('outer error');
  }, 10_000);
});

describe('unsupported value types', () => {
  let mod: typeof import('../../../src/utils/logger');
  const MARKER = `types_test_${process.pid}`;

  beforeAll(() => {
    jest.resetModules();
    jest.unmock('../../../src/utils/logger');
    mod = require('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger();
  });

  it('safely serializes BigInt, Symbol, Buffer, and Function', async () => {
    const meta = {
      marker: MARKER,
      big: BigInt(9007199254740991),
      sym: Symbol('test-symbol'),
      buf: Buffer.from('hello'),
      fn: function myFunc() {},
    };

    expect(() => mod.logger.error(MARKER, meta)).not.toThrow();
    await mod.flushLogger(8000);

    // Allow a brief window for OS-level flush after fdatasync
    await new Promise((resolve) => setTimeout(resolve, 200));

    const entries = jsonLinesContaining('error.log', MARKER);
    expect(entries.length).toBeGreaterThan(0);
    const entry = entries[entries.length - 1];
    expect(entry.big).toBe('9007199254740991');
    expect(entry.sym).toBe('Symbol(test-symbol)');
    expect(entry.buf).toContain('Buffer');
    expect(entry.fn).toContain('Function');
    expect(entry.fn).toContain('myFunc');
  }, 10_000);
});

describe('worker crash + restart', () => {
  let mod: typeof import('../../../src/utils/logger');
  const MARKER = `after_restart_${process.pid}`;

  beforeAll(() => {
    jest.resetModules();
    jest.unmock('../../../src/utils/logger');
    mod = require('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger().catch(() => {});
  });

  it('restarts the worker after a crash and continues logging', async () => {
    // Log something to ensure worker is started
    mod.logger.error('pre_crash_warmup');
    await mod.flushLogger(5000);

    const worker = mod._testInternals.getWorker();
    expect(worker).toBeDefined();

    const restartsBefore = mod._testInternals.getWorkerRestarts();
    worker!.terminate();

    // Wait for exit handler + restart
    await new Promise((resolve) => setTimeout(resolve, 1000));

    const restartsAfter = mod._testInternals.getWorkerRestarts();
    expect(restartsAfter).toBeGreaterThan(restartsBefore);

    // Verify the new worker accepts logs
    mod.logger.error(MARKER, { recovered: true });
    await mod.flushLogger(5000);

    const stats = mod.getLoggerStats();
    expect(stats.worker_alive).toBe(true);

    const lines = linesContaining('error.log', MARKER);
    expect(lines.length).toBeGreaterThan(0);
  }, 15_000);
});

describe('worker crash + fallback to stderr', () => {
  let mod: typeof import('../../../src/utils/logger');

  beforeAll(() => {
    jest.resetModules();
    jest.unmock('../../../src/utils/logger');
    mod = require('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger().catch(() => {});
  });

  it('falls back to stderr after exceeding restart limit', async () => {
    // Start the worker
    mod.logger.error('warmup_for_fallback');
    await mod.flushLogger(5000);

    // First crash — triggers restart
    const worker1 = mod._testInternals.getWorker();
    expect(worker1).toBeDefined();
    worker1!.terminate();
    await new Promise((resolve) => setTimeout(resolve, 1000));

    expect(mod._testInternals.getWorkerRestarts()).toBe(1);
    expect(mod._testInternals.isStderrOnly()).toBe(false);

    // Second crash — should exceed restart limit (limit is 1)
    const worker2 = mod._testInternals.getWorker();
    expect(worker2).toBeDefined();
    worker2!.terminate();
    await new Promise((resolve) => setTimeout(resolve, 1000));

    expect(mod._testInternals.isStderrOnly()).toBe(true);

    const stats = mod.getLoggerStats();
    expect(stats.worker_alive).toBe(false);

    // Logs should go to stderr
    const stderrSpy = jest.spyOn(process.stderr, 'write');
    mod.logger.error('stderr.fallback.test', { mode: 'fallback' });
    const stderrCalls = stderrSpy.mock.calls.map((c) => String(c[0]));
    expect(stderrCalls.some((s) => s.includes('stderr.fallback.test'))).toBe(true);
    stderrSpy.mockRestore();
  }, 20_000);
});

describe('shutdown timeout', () => {
  let mod: typeof import('../../../src/utils/logger');

  beforeAll(() => {
    jest.resetModules();
    jest.unmock('../../../src/utils/logger');
    mod = require('../../../src/utils/logger');
  });

  it('resolves even when called with a very short timeout', async () => {
    mod.logger.error('shutdown_timeout_warmup');
    const start = Date.now();
    await mod.shutdownLogger(1).catch(() => {});
    const elapsed = Date.now() - start;
    expect(elapsed).toBeLessThan(3000);
  }, 5_000);
});

describe('flush timeout', () => {
  let mod: typeof import('../../../src/utils/logger');

  beforeAll(() => {
    jest.resetModules();
    jest.unmock('../../../src/utils/logger');
    mod = require('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger().catch(() => {});
  });

  it('resolves or rejects with 1ms timeout rather than hanging', async () => {
    mod.logger.error('flush_timeout_warmup');
    const start = Date.now();
    await mod.flushLogger(1).catch(() => {});
    const elapsed = Date.now() - start;
    expect(elapsed).toBeLessThan(3000);
  }, 5_000);
});

describe('concurrent burst ordering', () => {
  let mod: typeof import('../../../src/utils/logger');
  const MARKER = `ordering_${process.pid}_${Date.now()}`;

  beforeAll(() => {
    jest.resetModules();
    process.env.LOG_LEVEL = 'debug';
    mod = jest.requireActual('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger();
  });

  it('preserves ordering of 100 sequentially numbered messages', async () => {
    for (let i = 0; i < 100; i++) {
      mod.logger.error(MARKER, { seq: i });
    }

    await mod.flushLogger(10_000);

    const entries = jsonLinesContaining('error.log', MARKER);
    const seqs = entries.map((e) => e.seq as number).filter((s) => typeof s === 'number');

    expect(seqs.length).toBe(100);
    for (let i = 0; i < seqs.length - 1; i++) {
      expect(seqs[i]).toBeLessThan(seqs[i + 1]);
    }
  }, 15_000);
});

describe('queue full with only debug events', () => {
  let mod: typeof import('../../../src/utils/logger');

  beforeAll(() => {
    jest.resetModules();
    process.env.LOG_LEVEL = 'debug';
    mod = jest.requireActual('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger().catch(() => {});
  });

  it('drops debug events when queue is full without throwing', () => {
    for (let i = 0; i < 600; i++) {
      expect(() => mod.logger.debug('debug.fill', { i })).not.toThrow();
    }

    const stats = mod.getLoggerStats();
    expect(stats.events_dropped.debug).toBeGreaterThan(0);
    expect(stats.events_dropped.error).toBe(0);
  }, 10_000);
});

describe('priority eviction edge cases', () => {
  let mod: typeof import('../../../src/utils/logger');

  beforeAll(() => {
    jest.resetModules();
    process.env.LOG_LEVEL = 'debug';
    mod = jest.requireActual('../../../src/utils/logger');
  });

  afterAll(async () => {
    await mod.shutdownLogger().catch(() => {});
  });

  it('drops error when queue is full of info (no debug to evict)', () => {
    // Fill queue with info events
    for (let i = 0; i < 550; i++) {
      mod.logger.info('info.fill', { i });
    }

    // Now try to log an error — no debug events to evict
    mod.logger.error('error.no_room');

    const stats = mod.getLoggerStats();
    // The error gets dropped because queue is full and there's no debug to evict
    expect(stats.events_dropped.error).toBeGreaterThan(0);
  }, 10_000);
});
