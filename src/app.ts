// src/app.ts
import 'dotenv/config'; // loads .env variables at startup
import express, { Request, Response, NextFunction } from 'express';
import { logger } from './utils/logger';
import { TelegramBotService } from './services/telegram/telegram-bot.service';
import { createWebhookRouter } from './controllers/webhook.controller';

// 1. Load configuration from .env
const BOT_TOKEN = process.env.BOT_TOKEN!;
const NGROK_URL = process.env.NGROK_URL!; // Use this as your webhook base (can be set at runtime)
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN!;

// 2. Initialize Telegram bot service
const botService = new TelegramBotService(BOT_TOKEN);

// 3. (Optional, for development) Setup webhook to point Telegram to your ngrok URL
// This only needs to be run ONCE per new URL.
// You may want to protect this behind an env variable or CLI command in production.
(async () => {
  try {
    await botService.setupWebhook(NGROK_URL, TELEGRAM_SECRET_TOKEN);
  } catch (err) {
    logger.error('Error setting up webhook:', err);
  }
})();

// 4. Create Express app and mount routes
const app = express();
app.use(express.json());

// Health check endpoint
app.get('/ping', (_req: Request, res: Response, _next: NextFunction) => {
  res.json({ status: 'ok' });
});
// Telegram webhook endpoint (secured by secret)
app.use(createWebhookRouter(botService));

// 5. Start server
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  logger.info(`Server started at http://localhost:${PORT}`);
  logger.info(`Waiting for Telegram updates on /webhook/:secret`);
});
