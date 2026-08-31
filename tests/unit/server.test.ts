interface BootOptions {
  databaseReadiness: Promise<unknown>;
  agentContractReadiness: Promise<unknown>;
  ffmpegReadiness: Promise<unknown>;
}

// Boots src/server.ts against mocked app.ts exports, a mocked express app, and a mocked
// fetch, and hands back the seams the startup barrier is asserted on. Route handlers are
// captured so /health can be invoked directly without a live listener.
async function bootServer(options: BootOptions) {
  const routes = new Map<string, (req: unknown, res: unknown, next?: unknown) => unknown>();
  const listen = jest.fn((_port, callback) => {
    callback?.();
    return { close: jest.fn() };
  });
  const expressApp = {
    use: jest.fn(),
    get: jest.fn((path: string, handler: (req: unknown, res: unknown) => unknown) => {
      routes.set(path, handler);
    }),
    listen,
  };
  const expressFactory = Object.assign(
    jest.fn(() => expressApp),
    { json: jest.fn(() => jest.fn()) },
  );
  const setupWebhook = jest.fn();

  jest.doMock('../../src/app', () => ({
    botService: { setupWebhook, stop: jest.fn().mockResolvedValue(undefined) },
    databaseReadiness: options.databaseReadiness,
    agentContractReadiness: options.agentContractReadiness,
    ffmpegReadiness: options.ffmpegReadiness,
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
  // startServer() rejection funnels into serverStartup's catch, which exits the process.
  const exit = jest.spyOn(process, 'exit').mockImplementation((() => undefined) as never);

  const serverModule = await import('../../src/server');
  return { serverModule, listen, setupWebhook, routes, exit };
}

interface HealthBody {
  status: string;
  dependencies: Record<string, string>;
}

/** Invokes a captured Express handler and returns the status/body it produced. */
async function callRoute(
  handler: (req: unknown, res: unknown) => unknown,
): Promise<{ status: number; body: HealthBody }> {
  let status = 200;
  let body = {} as HealthBody;
  const res = {
    status(code: number) {
      status = code;
      return res;
    },
    json(payload: unknown) {
      body = payload as HealthBody;
      return res;
    },
  };
  await handler({}, res);
  return { status, body };
}

/** A pre-rejected promise with its rejection already handled, so importing never warns. */
function rejected(message: string): Promise<never> {
  const promise = Promise.reject(new Error(message));
  promise.catch(() => undefined);
  return promise;
}

describe('server startup readiness barrier', () => {
  const originalSkipWebhook = process.env.TELEGRAM_SKIP_WEBHOOK_SETUP;
  const originalAgentUrl = process.env.LANGGRAPH_AGENT_URL;

  beforeEach(() => {
    process.env.LANGGRAPH_AGENT_URL = 'http://localhost:2024';
    global.fetch = jest
      .fn()
      .mockResolvedValue({ ok: true, status: 200 }) as unknown as typeof fetch;
  });

  afterEach(() => {
    jest.restoreAllMocks();
    jest.resetModules();
    if (originalSkipWebhook === undefined) {
      delete process.env.TELEGRAM_SKIP_WEBHOOK_SETUP;
    } else {
      process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = originalSkipWebhook;
    }
    if (originalAgentUrl === undefined) {
      delete process.env.LANGGRAPH_AGENT_URL;
    } else {
      process.env.LANGGRAPH_AGENT_URL = originalAgentUrl;
    }
  });

  it('does not listen until database and agent-contract readiness have completed', async () => {
    process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = 'true';
    let resolveReadiness!: () => void;
    const databaseReadiness = new Promise<void>((resolve) => {
      resolveReadiness = resolve;
    });
    let resolveAgentReadiness!: () => void;
    const agentContractReadiness = new Promise<void>((resolve) => {
      resolveAgentReadiness = resolve;
    });

    const { serverModule, listen, setupWebhook } = await bootServer({
      databaseReadiness,
      agentContractReadiness,
      ffmpegReadiness: Promise.resolve(true),
    });

    await Promise.resolve();
    expect(listen).not.toHaveBeenCalled();
    expect(setupWebhook).not.toHaveBeenCalled();

    resolveReadiness();
    await Promise.resolve();
    expect(listen).not.toHaveBeenCalled();

    resolveAgentReadiness();
    await serverModule.serverStartup;

    expect(listen).toHaveBeenCalledTimes(1);
    expect(setupWebhook).not.toHaveBeenCalled();
  });

  it('does not register the webhook or listen until FFmpeg readiness has completed', async () => {
    delete process.env.TELEGRAM_SKIP_WEBHOOK_SETUP;
    let resolveFfmpeg!: (value: boolean) => void;
    const ffmpegReadiness = new Promise<boolean>((resolve) => {
      resolveFfmpeg = resolve;
    });

    const { serverModule, listen, setupWebhook } = await bootServer({
      databaseReadiness: Promise.resolve(),
      agentContractReadiness: Promise.resolve(),
      ffmpegReadiness,
    });

    // Both other barriers are already settled; only FFmpeg is outstanding. Drain the whole
    // microtask queue so this would fail if ffmpegReadiness were not part of the barrier.
    await new Promise((resolve) => setImmediate(resolve));
    expect(setupWebhook).not.toHaveBeenCalled();
    expect(listen).not.toHaveBeenCalled();

    resolveFfmpeg(true);
    await serverModule.serverStartup;

    expect(setupWebhook).toHaveBeenCalledTimes(1);
    expect(listen).toHaveBeenCalledTimes(1);
  });

  it('fails startup without registering the webhook when FFmpeg readiness rejects', async () => {
    delete process.env.TELEGRAM_SKIP_WEBHOOK_SETUP;

    const { serverModule, listen, setupWebhook } = await bootServer({
      databaseReadiness: Promise.resolve(),
      agentContractReadiness: Promise.resolve(),
      ffmpegReadiness: rejected('FFmpeg is not available.'),
    });

    await expect(serverModule.startServer()).rejects.toThrow('FFmpeg is not available.');
    await serverModule.serverStartup;

    expect(setupWebhook).not.toHaveBeenCalled();
    expect(listen).not.toHaveBeenCalled();
  });

  it('reports ffmpeg ok on /health when readiness resolves', async () => {
    process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = 'true';

    const { serverModule, routes } = await bootServer({
      databaseReadiness: Promise.resolve(),
      agentContractReadiness: Promise.resolve(),
      ffmpegReadiness: Promise.resolve(true),
    });
    await serverModule.serverStartup;

    const health = routes.get('/health');
    expect(health).toBeDefined();
    const { status, body } = await callRoute(health!);

    expect(body.dependencies.ffmpeg).toBe('ok');
    expect(body.status).toBe('healthy');
    expect(status).toBe(200);
  });

  it('degrades /health to 503 with ffmpeg not ready when readiness rejects', async () => {
    process.env.TELEGRAM_SKIP_WEBHOOK_SETUP = 'true';

    const { serverModule, routes } = await bootServer({
      databaseReadiness: Promise.resolve(),
      agentContractReadiness: Promise.resolve(),
      ffmpegReadiness: rejected('FFmpeg is not available.'),
    });
    await serverModule.serverStartup;

    const { status, body } = await callRoute(routes.get('/health')!);

    expect(body.dependencies.ffmpeg).toBe('not ready');
    expect(body.dependencies.database).toBe('ok');
    expect(body.status).toBe('degraded');
    expect(status).toBe(503);
  });
});
