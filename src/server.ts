// src/server.ts — HTTP entry point for the application.
// Responsibilities: registers the Telegram webhook, creates the Express server with
// the webhook route and a health-check endpoint, and handles graceful shutdown.
// Importing ./app triggers env validation and service construction before we get here.

import express, { Request, Response, NextFunction } from 'express';
import { flushLogger, getLoggerStats, logger, shutdownLogger } from './utils/logger';
import { createWebhookRouter } from './controllers/webhook.controller';
import { agentContractReadiness, botService, databaseReadiness } from './app';

const NGROK_URL = process.env.NGROK_URL!;
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN!;
const SKIP_WEBHOOK_SETUP = process.env.TELEGRAM_SKIP_WEBHOOK_SETUP === 'true';

// --- Express application setup ---

const app = express();
app.use(express.json());

// Simple liveness probe.
app.get('/ping', (_req: Request, res: Response, _next: NextFunction) => {
  res.json({ status: 'ok' });
});

// Readiness probe — checks downstream dependencies.
const LANGGRAPH_AGENT_URL = process.env.LANGGRAPH_AGENT_URL!;
const HEALTH_CHECK_TIMEOUT_MS = 5000;

app.get('/health', async (_req: Request, res: Response, _next: NextFunction) => {
  const dependencies: Record<string, string> = {};

  try {
    await databaseReadiness;
    dependencies.database = 'ok';
  } catch {
    dependencies.database = 'not ready';
  }

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), HEALTH_CHECK_TIMEOUT_MS);
    const langGraphRes = await fetch(`${LANGGRAPH_AGENT_URL.replace(/\/+$/, '')}/health`, {
      signal: controller.signal,
    });
    clearTimeout(timeout);
    dependencies.langgraph = langGraphRes.ok ? 'ok' : `error (${langGraphRes.status})`;
  } catch {
    dependencies.langgraph = 'unreachable';
  }

  const loggerStats = getLoggerStats();
  const LOGGER_FAILURE_WINDOW_MS = 5 * 60 * 1000;
  const recentFailure = loggerStats.last_write_failure_time
    ? Date.now() - new Date(loggerStats.last_write_failure_time).getTime() < LOGGER_FAILURE_WINDOW_MS
    : false;
  const loggerHealthy = loggerStats.worker_alive && !recentFailure;
  const healthy = Object.values(dependencies).every((v) => v === 'ok') && loggerHealthy;
  res.status(healthy ? 200 : 503).json({
    status: healthy ? 'healthy' : 'degraded',
    dependencies,
    log_worker: {
      healthy: loggerHealthy,
      stats: loggerStats,
    },
  });
});

// Mount the Telegram webhook route at POST /webhook/:secret.
app.use(createWebhookRouter(botService));

// Global error-handling middleware — catches unhandled errors from any route.
app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
  logger.error('server.unhandled_error', { error: err.message, stack: err.stack });
  if (!res.headersSent) {
    res.status(500).json({ error: 'Internal Server Error' });
  }
});

// --- Start listening ---

const PORT = process.env.PORT || 3000;
let server: ReturnType<typeof app.listen> | undefined;

// Database migrations and the live agent timeout contract are startup barriers.
// Do not accept webhooks with an unsafe schema or an inverted timeout ladder.
export async function startServer(): Promise<void> {
  await Promise.all([databaseReadiness, agentContractReadiness]);
  if (SKIP_WEBHOOK_SETUP) {
    logger.info('telegram.webhook.setup_skipped');
  } else {
    await botService.setupWebhook(NGROK_URL, TELEGRAM_SECRET_TOKEN);
  }

  server = app.listen(PORT, () => {
    logger.info('server.started', { url: `http://localhost:${PORT}` });
    logger.info('telegram.webhook.awaiting_updates', { path: '/webhook/:secret' });
  });
}

export const serverStartup = startServer().catch((err) => {
  logger.error('server.startup_failed', {
    error: (err as Error).message,
  });
  process.exit(1);
});

// Graceful shutdown: close the HTTP server, stop the bot, then exit.
const SHUTDOWN_TIMEOUT_MS = 10_000;

function shutdown(signal: string) {
  logger.info('server.shutdown.started', { signal });
  const forceExit = setTimeout(() => {
    logger.warn('server.shutdown.forced', { signal, timeoutMs: SHUTDOWN_TIMEOUT_MS });
    process.exit(1);
  }, SHUTDOWN_TIMEOUT_MS);
  forceExit.unref();

  const stopBotAndExit = () => {
    botService.stop().finally(async () => {
      logger.info('server.shutdown.completed', { signal });
      try {
        await flushLogger();
        await shutdownLogger();
      } catch (error) {
        process.stderr.write(`logger.shutdown.failed ${String(error)}\n`);
      }
      process.exit(0);
    });
  };
  if (server) {
    server.close(stopBotAndExit);
  } else {
    stopBotAndExit();
  }
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

// Detached async work runs outside Express's error boundary. Keep an unexpected best-effort
// rejection from silently killing the bot under nodemon, and leave a durable diagnostic trail.
process.on('unhandledRejection', (reason) => {
  logger.error('process.unhandled_rejection', {
    error: reason instanceof Error ? reason.message : String(reason),
    stack: reason instanceof Error ? reason.stack : undefined,
  });
});

// There is no process supervisor in the development setup: nodemon waits for a file change after a
// crash. Log synchronous failures and keep serving; revisit the recovery policy if supervision is
// added, since an uncaught exception can leave arbitrary application state inconsistent.
process.on('uncaughtException', (error) => {
  logger.error('process.uncaught_exception', {
    error: error.message,
    stack: error.stack,
  });
});
