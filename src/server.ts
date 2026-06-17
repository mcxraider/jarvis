// src/server.ts — Express setup, webhook registration, graceful shutdown
import express, { Request, Response, NextFunction } from 'express';
import { logger } from './utils/logger';
import { createWebhookRouter } from './controllers/webhook.controller';
import { botService } from './app';

const NGROK_URL = process.env.NGROK_URL!;
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN!;

// Register webhook with Telegram (runs once per URL; safe to call on every start)
(async () => {
  try {
    await botService.setupWebhook(NGROK_URL, TELEGRAM_SECRET_TOKEN);
  } catch (err) {
    logger.error('telegram.webhook.setup_failed', {
      error: (err as Error).message,
    });
  }
})();

// Express app
const app = express();
app.use(express.json());

app.get('/ping', (_req: Request, res: Response, _next: NextFunction) => {
  res.json({ status: 'ok' });
});

app.use(createWebhookRouter(botService));

// Start listening
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  logger.info('server.started', { url: `http://localhost:${PORT}` });
  logger.info('telegram.webhook.awaiting_updates', { path: '/webhook/:secret' });
});

// Graceful shutdown
process.on('SIGTERM', () => {
  logger.info('server.shutdown.started', { signal: 'SIGTERM' });
  process.exit(0);
});
