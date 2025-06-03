// tests/integration/telegram-integration.test.ts
import { TelegramBotService } from '../../src/services/telegram/telegram-bot.service';
import { MessageProcessorService } from '../../src/services/telegram/message-processor.service';
import { TelegramConfig } from '../../src/types/telegram.types';

import dotenv from 'dotenv';

// Load environment variables for testing
dotenv.config();
const BOT_TOKEN = process.env.BOT_TOKEN!;
const NGROK_URL = process.env.NGROK_URL!; // Use this as your webhook base (can be set at runtime)
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN!;

// 2. Initialize services
const telegramConfig: TelegramConfig = {
token: BOT_TOKEN,
webhookUrl: NGROK_URL,
secretToken: TELEGRAM_SECRET_TOKEN
};

const messageProcessor = new MessageProcessorService();


describe('Telegram Integration Tests', () => {
  let botService: TelegramBotService;
  const TEST_CHAT_ID = process.env.TEST_CHAT_ID; // Your Telegram user ID for testing

  beforeAll(() => {
    // Skip integration tests if no bot token is provided
    if (!process.env.BOT_TOKEN) {
      console.log('⚠️  BOT_TOKEN not found - skipping integration tests');
      return;
    }

    if (!TEST_CHAT_ID) {
      console.log('⚠️  TEST_CHAT_ID not found - skipping integration tests');
      return;
    }

    botService = new TelegramBotService(telegramConfig, messageProcessor);
  });

  beforeEach(() => {
    // Skip if no bot service (missing env vars)
    if (!botService) {
      pending('Integration test skipped - missing environment variables');
    }
  });

  it('should get bot information', async () => {
    const botInfo = await botService.getBotInfo();

    expect(botInfo).toHaveProperty('id');
    expect(botInfo).toHaveProperty('username');
    expect(botInfo).toHaveProperty('first_name');
    expect(botInfo.is_bot).toBe(true);

    console.log(`✅ Bot Info: @${botInfo.username} (${botInfo.first_name})`);
  }, 10000);

  it('should send a test message', async () => {
    const testMessage = `🧪 Integration test message - ${new Date().toISOString()}`;

    const result = await botService.sendMessage(
      parseInt(TEST_CHAT_ID!),
      testMessage
    );

    expect(result).toHaveProperty('message_id');
    expect(result).toHaveProperty('text', testMessage);
    expect(result.chat.id).toBe(parseInt(TEST_CHAT_ID!));

    console.log(`✅ Message sent successfully with ID: ${result.message_id}`);
  }, 10000);

  it('should handle errors gracefully', async () => {
    const invalidChatId = -1;
    const testMessage = 'This should fail';

    await expect(
      botService.sendMessage(invalidChatId, testMessage)
    ).rejects.toThrow();

    console.log('✅ Error handling works correctly');
  }, 10000);
});

// Helper to run integration tests only when explicitly requested
const runIntegrationTests = process.env.RUN_INTEGRATION_TESTS === 'true';

if (!runIntegrationTests) {
  describe.skip('Integration Tests', () => {
    it('Integration tests skipped', () => {
      console.log('ℹ️  Run with RUN_INTEGRATION_TESTS=true to enable integration tests');
    });
  });
}
