// src/controllers/webhook.controller.ts — Express route that receives Telegram
// webhook updates. Validates the secret token path parameter against the configured
// secret, then delegates the parsed update body to TelegramBotService.handleUpdate().
// Telegram update IDs are stable across redelivery, so they also serve as the
// request idempotency identity passed through to the Python agent.

import express from 'express';
import type { TelegramBotService } from '../services/telegram/telegram-bot.service';
import { createRequestId, logger } from '../utils/logger';

interface TelegramWebhookUpdate {
  message?: Record<string, unknown>;
}

/**
 * Creates and returns an Express router with the Telegram webhook endpoint.
 * The route pattern is POST /webhook/:secret — the :secret param must match
 * the TELEGRAM_SECRET_TOKEN env var, providing an extra auth layer on top of HTTPS.
 */
export function createWebhookRouter(botService: TelegramBotService) {
  const router = express.Router();

  router.post('/webhook/:secret', express.json(), async (req, res): Promise<void> => {
    const startedAt = Date.now();
    const requestId = telegramRequestId(req.body?.update_id);

    // Validate the webhook secret before processing — reject early if mismatched.
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

    const update = {
      ...req.body,
      __requestId: requestId,
    };

    logger.info('telegram.webhook.received', {
      requestId,
      updateId: req.body?.update_id,
      messageType: getTelegramMessageType(req.body),
    });

    // Respond 200 immediately so Telegram doesn't retry on slow processing.
    res.sendStatus(200);

    botService.handleUpdate(update).catch((err) => {
      logger.error('telegram.webhook.background_failed', {
        requestId,
        updateId: req.body?.update_id,
        error: (err as Error).message,
        stack: (err as Error).stack,
        durationMs: Date.now() - startedAt,
      });
    });
  });

  return router;
}

function telegramRequestId(updateId: unknown): string {
  if (typeof updateId === 'number' && Number.isSafeInteger(updateId)) {
    return `tg_update_${updateId}`;
  }
  return createRequestId('tg');
}

// Inspects the raw Telegram update body to determine the message media type for logging.
function getTelegramMessageType(update: TelegramWebhookUpdate): string {
  const message = update?.message;
  if (!message) return 'unknown';
  if ('text' in message) return 'text';
  if ('voice' in message) return 'voice';
  if ('audio' in message) return 'audio';
  if ('document' in message) return 'document';
  return 'unknown';
}
