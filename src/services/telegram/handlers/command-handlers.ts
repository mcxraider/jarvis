// src/services/telegram/handlers/command-handlers.ts
import { Context } from 'telegraf';
import { logger } from '../../../utils/logger';

/**
 * Handles bot commands
 */
export class CommandHandlers {
  async handleStart(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    const username = ctx.from?.username;

    logger.info('User started bot', { userId, username });

    const name = ctx.from?.first_name ?? 'there';
    await ctx.reply(`Hey ${name}! I'm Jarvis, your personal assistant. Send me a message or try /help.`);
  }

  async handleHelp(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested help', { userId });

    const helpMessage = `🆘 *JarvisMCP Help*

*Available Commands:*
/start - Start the bot
/help - Show this help message
/status - Check bot status

*Features:*
📝 Send me any text message and I'll process it
🎵 Send me audio files (.ogg, .mp3, .wav) and I'll process them
🔊 Send me voice messages and I'll handle them

*Supported Audio Formats:*
• OGG Vorbis (Telegram voice messages)
• MP3
• WAV
• M4A`;

    await ctx.reply(helpMessage, { parse_mode: 'Markdown' });
  }

  async handleStatus(ctx: Context): Promise<void> {
    const userId = ctx.from?.id;
    logger.info('User requested status', { userId });

    await ctx.reply('✅ Bot is running and ready to process your messages!');
  }
}
