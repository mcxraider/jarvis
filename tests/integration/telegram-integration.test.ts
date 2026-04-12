import dotenv from 'dotenv';

dotenv.config();

const runIntegrationTests =
  process.env.RUN_INTEGRATION_TESTS === 'true' &&
  !!process.env.BOT_TOKEN &&
  !!process.env.NGROK_URL &&
  !!process.env.TELEGRAM_SECRET_TOKEN &&
  !!process.env.TEST_CHAT_ID;

const describeIntegration = runIntegrationTests ? describe : describe.skip;

describeIntegration('Telegram Integration Tests', () => {
  let botService: import('../../src/services/telegram/telegram-bot.service').TelegramBotService;
  const testChatId = Number(process.env.TEST_CHAT_ID);

  beforeAll(async () => {
    const [{ TelegramBotService }, { MessageProcessorService }] = await Promise.all([
      import('../../src/services/telegram/telegram-bot.service'),
      import('../../src/services/telegram/message-processor.service'),
    ]);

    const telegramConfig = {
      token: process.env.BOT_TOKEN!,
      webhookUrl: process.env.NGROK_URL!,
      secretToken: process.env.TELEGRAM_SECRET_TOKEN!,
    };

    botService = new TelegramBotService(telegramConfig, new MessageProcessorService());
  });

  it(
    'gets bot information',
    async () => {
      const botInfo = await botService.getBotInfo();

      expect(botInfo).toHaveProperty('id');
      expect(botInfo).toHaveProperty('username');
      expect(botInfo).toHaveProperty('first_name');
      expect(botInfo.is_bot).toBe(true);
    },
    10000,
  );

  it(
    'sends a test message',
    async () => {
      const testMessage = `Integration test message - ${new Date().toISOString()}`;
      const result = await botService.sendMessage(testChatId, testMessage);

      expect(result).toHaveProperty('message_id');
      expect(result).toHaveProperty('text', testMessage);
      expect(result.chat.id).toBe(testChatId);
    },
    10000,
  );

  it(
    'propagates Telegram API errors',
    async () => {
      await expect(botService.sendMessage(-1, 'This should fail')).rejects.toThrow();
    },
    10000,
  );
});
