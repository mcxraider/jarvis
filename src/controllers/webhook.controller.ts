// services/webhook.controller.ts
import express from 'express';
import { TelegramBotService } from '../services/telegram/telegram-bot.service';
import { logger } from '../utils/logger';

export function createWebhookRouter(botService: TelegramBotService) {
  const router = express.Router();

  router.post('/webhook/:secret', express.json(), async (req, res): Promise<void> => {
    const secret = req.params.secret;
    if (!secret || secret !== process.env.TELEGRAM_SECRET_TOKEN) {
      logger.warn('Unauthorized webhook attempt', { ip: req.ip, secret });
      res.sendStatus(401);
      return;
    }
    try {
      await botService.handleUpdate(req.body);
      res.sendStatus(200);
      return;
    } catch (err) {
      logger.error('Webhook handler failed', { error: (err as Error).message, stack: (err as Error).stack });
      res.status(500).json({ error: 'Internal Server Error' });
      return;
    }
  });

  return router;
}
