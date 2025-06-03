// tests/telegram.test.ts
import request from 'supertest';
import express from 'express';
import { TelegramBotService } from '../src/services/telegram/telegram-bot.service';
import { createWebhookRouter } from '../src/controllers/webhook.controller';

// Use a mock token
const MOCK_BOT_TOKEN = '123:abc';
const MOCK_SECRET = 'testsecret';

describe('TelegramBotService', () => {
  let botService: TelegramBotService;

  beforeEach(() => {
    botService = new TelegramBotService(MOCK_BOT_TOKEN);

    // Mock bot methods
    botService.bot.telegram.sendMessage = jest.fn().mockResolvedValue({ message_id: 1 });
    botService.bot.handleUpdate = jest.fn().mockResolvedValue(undefined);
  });

  it('should send a message', async () => {
    const result = await botService.sendMessage(42, 'Hello!');
    expect(result).toHaveProperty('message_id', 1);
    expect(botService.bot.telegram.sendMessage).toHaveBeenCalledWith(42, 'Hello!');
  });

  it('should handle update', async () => {
    const fakeUpdate = { update_id: 1, message: { text: 'hi' } };
    await expect(botService.handleUpdate(fakeUpdate)).resolves.not.toThrow();
    expect(botService.bot.handleUpdate).toHaveBeenCalledWith(fakeUpdate);
  });

  it('should throw and log error if sendMessage fails', async () => {
    (botService.bot.telegram.sendMessage as jest.Mock).mockRejectedValue(new Error('fail'));
    await expect(botService.sendMessage(1, 'fail')).rejects.toThrow('fail');
  });
});

describe('WebhookController', () => {
  let app: express.Express;
  let botService: TelegramBotService;

  beforeEach(() => {
    botService = new TelegramBotService(MOCK_BOT_TOKEN);
    botService.bot.handleUpdate = jest.fn().mockResolvedValue(undefined);
    process.env.TELEGRAM_SECRET_TOKEN = MOCK_SECRET;
    app = express();
    app.use(createWebhookRouter(botService));
  });

  it('should return 401 if secret is wrong', async () => {
    await request(app)
      .post('/webhook/wrongsecret')
      .send({})
      .expect(401);
  });

  it('should handle valid webhook', async () => {
    await request(app)
      .post(`/webhook/${MOCK_SECRET}`)
      .send({ update_id: 1 })
      .expect(200);

    expect(botService.bot.handleUpdate).toHaveBeenCalledWith({ update_id: 1 });
  });

  it('should handle and log error if handleUpdate fails', async () => {
    (botService.bot.handleUpdate as jest.Mock).mockRejectedValue(new Error('update fail'));
    await request(app)
      .post(`/webhook/${MOCK_SECRET}`)
      .send({ update_id: 2 })
      .expect(500);
  });
});
