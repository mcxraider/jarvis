describe('server startup readiness barrier', () => {
  const originalSkipWebhook = process.env.TELEGRAM_SKIP_WEBHOOK_SETUP;

  afterEach(() => {
    jest.restoreAllMocks();
    jest.resetModules();
    if (originalSkipWebhook === undefined) {
      delete process.env.TELEGRAM_SKIP_WEBHOOK_SETUP;
    } else {
      process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = originalSkipWebhook;
    }
  });

  it('does not listen until database readiness has completed', async () => {
    process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = 'true';
    let resolveReadiness!: () => void;
    const databaseReadiness = new Promise<void>((resolve) => {
      resolveReadiness = resolve;
    });
    const listen = jest.fn((_port, callback) => {
      callback?.();
      return { close: jest.fn() };
    });
    const expressApp = {
      use: jest.fn(),
      get: jest.fn(),
      listen,
    };
    const expressFactory = Object.assign(jest.fn(() => expressApp), {
      json: jest.fn(() => jest.fn()),
    });
    const setupWebhook = jest.fn();

    jest.doMock('../../src/app', () => ({
      botService: { setupWebhook, stop: jest.fn().mockResolvedValue(undefined) },
      databaseReadiness,
    }));
    jest.doMock('../../src/controllers/webhook.controller', () => ({
      createWebhookRouter: jest.fn(() => jest.fn()),
    }));
    jest.doMock('../../src/utils/logger', () => ({
      flushLogger: jest.fn().mockResolvedValue(undefined),
      getLoggerStats: jest.fn(() => ({ worker_alive: true })),
      logger: { info: jest.fn(), warn: jest.fn(), error: jest.fn() },
      shutdownLogger: jest.fn().mockResolvedValue(undefined),
    }));
    jest.doMock('express', () => ({
      __esModule: true,
      default: expressFactory,
    }));
    jest.spyOn(process, 'on').mockImplementation((() => process) as typeof process.on);

    const serverModule = await import('../../src/server');

    await Promise.resolve();
    expect(listen).not.toHaveBeenCalled();
    expect(setupWebhook).not.toHaveBeenCalled();

    resolveReadiness();
    await serverModule.serverStartup;

    expect(listen).toHaveBeenCalledTimes(1);
    expect(setupWebhook).not.toHaveBeenCalled();
  });
});
