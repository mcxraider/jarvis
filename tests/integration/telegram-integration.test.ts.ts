// tests/integration/telegram-integration.test.ts
import { TelegramBotService } from '../../src/services/telegram/telegram-bot.service';
import dotenv from 'dotenv';

// Load environment variables for testing
dotenv.config();

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

    botService = new TelegramBotService(process.env.BOT_TOKEN);
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

  it('should handle webhook setup (dry run)', async () => {
    // This test doesn't actually set the webhook, just tests the method exists
    const mockUrl = 'https://example.com';
    const mockSecret = 'test-secret';

    // We'll mock the actual API call to avoid changing webhook during tests
    const originalSetWebhook = botService.bot.telegram.setWebhook;
    botService.bot.telegram.setWebhook = jest.fn().mockResolvedValue(true);

    await expect(botService.setupWebhook(mockUrl, mockSecret)).resolves.not.toThrow();

    expect(botService.bot.telegram.setWebhook).toHaveBeenCalledWith(
      `${mockUrl}/webhook/${mockSecret}`,
      { secret_token: mockSecret }
    );

    // Restore original method
    botService.bot.telegram.setWebhook = originalSetWebhook;

    console.log('✅ Webhook setup method works correctly');
  });

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
