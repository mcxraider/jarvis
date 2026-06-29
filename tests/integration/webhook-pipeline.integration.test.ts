import express from 'express';
import request from 'supertest';
import { createWebhookRouter } from '../../src/controllers/webhook.controller';
import { MessageHandlers } from '../../src/services/telegram/handlers/message-handlers';
import { createTestRunLogger } from '../helpers/test-run-logger';

const logger = createTestRunLogger('integration-webhook-pipeline');

describe('Webhook pipeline integration', () => {
  const originalSecret = process.env.TELEGRAM_SECRET_TOKEN;

  afterEach(() => {
    if (originalSecret === undefined) {
      delete process.env.TELEGRAM_SECRET_TOKEN;
    } else {
      process.env.TELEGRAM_SECRET_TOKEN = originalSecret;
    }
  });

  afterAll(() => {
    logger.writeSummary();
  });

  it('passes a Telegram text update through the webhook and attempts a reply', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'test-secret';
    const reply = jest.fn().mockResolvedValue({ message_id: 10 });
    const editMessageText = jest.fn().mockResolvedValue(true);
    const messageProcessor = {
      processTextMessage: jest.fn().mockResolvedValue({
        response: 'Mocked Jarvis reply',
      }),
    } as any;
    const handlers = new MessageHandlers(
      {} as any,
      messageProcessor,
      { recordActivity: jest.fn() } as any,
    );
    const botService = {
      handleUpdate: jest.fn(async (update: any) => {
        await handlers.handleText({
          from: update.message.from,
          chat: update.message.chat,
          message: update.message,
          reply,
          telegram: { editMessageText },
        } as any);
      }),
    };
    const app = express();
    app.use(createWebhookRouter(botService as any));
    const update = {
      update_id: 9001,
      message: {
        message_id: 1,
        date: 1710000000,
        chat: { id: 123456, type: 'private' },
        from: { id: 123456, is_bot: false, first_name: 'Jerry' },
        text: 'Add a Todoist task to review invoices tomorrow',
      },
    };

    const response = await request(app).post('/webhook/test-secret').send(update);
    await botService.handleUpdate.mock.results[0]?.value;

    logger.logRequest('webhook_text_update', { update });
    logger.logResponse('webhook_text_update', {
      status: response.status,
      reply: reply.mock.calls[0],
    });
    expect(response.status).toBe(200);
    expect(botService.handleUpdate).toHaveBeenCalledWith(expect.objectContaining(update));
    expect(messageProcessor.processTextMessage).toHaveBeenCalledWith(
      'Add a Todoist task to review invoices tomorrow',
      123456,
      expect.objectContaining({ messageType: 'text' }),
      expect.any(Function),
      undefined,
    );
    expect(reply).toHaveBeenCalledWith('Mocked Jarvis reply', { parse_mode: 'MarkdownV2' });
  });

  it('returns 200 when the bot service denies a valid webhook update', async () => {
    process.env.TELEGRAM_SECRET_TOKEN = 'test-secret';
    const botService = {
      handleUpdate: jest.fn().mockResolvedValue(undefined),
    };
    const app = express();
    app.use(createWebhookRouter(botService as any));
    const update = {
      update_id: 9002,
      message: {
        message_id: 2,
        date: 1710000001,
        chat: { id: 999999, type: 'private' },
        from: { id: 999999, is_bot: false, first_name: 'Unknown' },
        text: 'Show me the calendar',
      },
    };

    const response = await request(app).post('/webhook/test-secret').send(update);

    expect(response.status).toBe(200);
    expect(botService.handleUpdate).toHaveBeenCalledWith(expect.objectContaining(update));
  });
});
