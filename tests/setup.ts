jest.mock('../src/utils/logger', () => ({
  createRequestId: jest.fn((prefix = 'req') => `${prefix}_test`),
  truncateForLog: jest.fn((value?: string, maxLength = 80) =>
    value && value.length > maxLength ? `${value.slice(0, maxLength)}...` : value,
  ),
  flushLogger: jest.fn().mockResolvedValue(undefined),
  shutdownLogger: jest.fn().mockResolvedValue(undefined),
  getLoggerStats: jest.fn(() => ({
    queue_depth: 0,
    queue_bytes_approx: 0,
    queue_high_water: 0,
    events_accepted: 0,
    events_dropped: { debug: 0, info: 0, warn: 0, error: 0 },
    worker_alive: true,
    worker_restarts: 0,
    writes_failed: 0,
    flush_count: 0,
    avg_flush_duration_ms: 0,
  })),
  logger: {
    info: jest.fn(),
    warn: jest.fn(),
    error: jest.fn(),
    debug: jest.fn(),
  },
}));
