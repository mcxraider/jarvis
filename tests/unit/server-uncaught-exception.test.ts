describe('uncaughtException flush timeout', () => {
  let processExit: jest.SpyInstance;
  let handlers: Record<string, (...args: any[]) => void>;

  beforeEach(() => {
    jest.useFakeTimers();
    handlers = {};
    processExit = jest.spyOn(process, 'exit').mockImplementation((() => {}) as any);
    jest.spyOn(process, 'on').mockImplementation(((event: string, handler: any) => {
      handlers[event] = handler;
      return process;
    }) as typeof process.on);
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
    jest.resetModules();
  });

  it('exits within 3s even if flushLogger never resolves', async () => {
    jest.doMock('../../src/app', () => ({
      botService: { setupWebhook: jest.fn(), stop: jest.fn().mockResolvedValue(undefined) },
      databaseReadiness: Promise.resolve(),
      agentContractReadiness: Promise.resolve(),
    }));
    jest.doMock('../../src/controllers/webhook.controller', () => ({
      createWebhookRouter: jest.fn(() => jest.fn()),
    }));
    jest.doMock('../../src/utils/logger', () => ({
      flushLogger: jest.fn(() => new Promise(() => {})), // never resolves
      getLoggerStats: jest.fn(() => ({ worker_alive: true })),
      logger: { info: jest.fn(), warn: jest.fn(), error: jest.fn() },
      shutdownLogger: jest.fn().mockResolvedValue(undefined),
    }));
    jest.doMock('express', () => {
      const app = { use: jest.fn(), get: jest.fn(), listen: jest.fn((_, cb) => { cb?.(); return { close: jest.fn() }; }) };
      return { __esModule: true, default: Object.assign(jest.fn(() => app), { json: jest.fn(() => jest.fn()) }) };
    });

    process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = 'true';
    await import('../../src/server');

    const handler = handlers['uncaughtException'];
    expect(handler).toBeDefined();

    handler(new Error('test crash'));

    // Flush hasn't resolved, timer hasn't fired — no exit yet
    await Promise.resolve();
    expect(processExit).not.toHaveBeenCalled();

    // Advance past the 3s timeout
    jest.advanceTimersByTime(3000);
    await Promise.resolve();
    await Promise.resolve();

    expect(processExit).toHaveBeenCalledWith(1);
  });

  it('exits immediately when flushLogger resolves fast', async () => {
    jest.doMock('../../src/app', () => ({
      botService: { setupWebhook: jest.fn(), stop: jest.fn().mockResolvedValue(undefined) },
      databaseReadiness: Promise.resolve(),
      agentContractReadiness: Promise.resolve(),
    }));
    jest.doMock('../../src/controllers/webhook.controller', () => ({
      createWebhookRouter: jest.fn(() => jest.fn()),
    }));
    jest.doMock('../../src/utils/logger', () => ({
      flushLogger: jest.fn(() => Promise.resolve()),
      getLoggerStats: jest.fn(() => ({ worker_alive: true })),
      logger: { info: jest.fn(), warn: jest.fn(), error: jest.fn() },
      shutdownLogger: jest.fn().mockResolvedValue(undefined),
    }));
    jest.doMock('express', () => {
      const app = { use: jest.fn(), get: jest.fn(), listen: jest.fn((_, cb) => { cb?.(); return { close: jest.fn() }; }) };
      return { __esModule: true, default: Object.assign(jest.fn(() => app), { json: jest.fn(() => jest.fn()) }) };
    });

    process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = 'true';
    await import('../../src/server');

    const handler = handlers['uncaughtException'];
    handler(new Error('test crash'));

    // Let microtasks settle (flush resolves immediately)
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(processExit).toHaveBeenCalledWith(1);
  });
});
