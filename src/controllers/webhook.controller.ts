// services/webhook.controller.ts
import express from 'express';
import type { TelegramBotService } from '../services/telegram/telegram-bot.service';
import { createRequestId, logger } from '../utils/logger';

interface TelegramWebhookUpdate {
  message?: Record<string, unknown>;
}

export function createWebhookRouter(botService: TelegramBotService) {
  const router = express.Router();

  router.post('/webhook/:secret', express.json(), async (req, res): Promise<void> => {
    const startedAt = Date.now();
    const requestId = createRequestId('tg');
    const secret = req.params.secret;
    if (!secret || secret !== process.env.TELEGRAM_SECRET_TOKEN) {
      logger.warn('telegram.webhook.rejected', {
        requestId,
        ip: req.ip,
        hasSecret: !!secret,
        secretMatches: false,
      });
      res.sendStatus(401);
      return;
    }
    try {
      const update = {
        ...req.body,
        __requestId: requestId,
      };

      logger.info('telegram.webhook.received', {
        requestId,
        updateId: req.body?.update_id,
        messageType: getTelegramMessageType(req.body),
      });

      await botService.handleUpdate(update);
      logger.info('telegram.webhook.completed', {
        requestId,
        updateId: req.body?.update_id,
        durationMs: Date.now() - startedAt,
        statusCode: 200,
      });
      res.sendStatus(200);
      return;
    } catch (err) {
      logger.error('telegram.webhook.failed', {
        requestId,
        updateId: req.body?.update_id,
        error: (err as Error).message,
        stack: (err as Error).stack,
        durationMs: Date.now() - startedAt,
      });
      res.status(500).json({ error: 'Internal Server Error' });
      return;
    }
  });

  return router;
}

function getTelegramMessageType(update: TelegramWebhookUpdate): string {
  const message = update?.message;
  if (!message) return 'unknown';
  if ('text' in message) return 'text';
  if ('voice' in message) return 'voice';
  if ('audio' in message) return 'audio';
  if ('document' in message) return 'document';
  return 'unknown';
}
