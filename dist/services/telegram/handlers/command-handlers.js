"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.CommandHandlers = void 0;
const logger_1 = require("../../../utils/logger");
/**
 * Handles bot commands
 */
class CommandHandlers {
    async handleStart(ctx) {
        var _a, _b, _c, _d;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        const username = (_b = ctx.from) === null || _b === void 0 ? void 0 : _b.username;
        logger_1.logger.info('User started bot', { userId, username });
        const name = (_d = (_c = ctx.from) === null || _c === void 0 ? void 0 : _c.first_name) !== null && _d !== void 0 ? _d : 'there';
        await ctx.reply(`Hey ${name}! I'm Jarvis, your personal assistant. Send me a message or try /help.`);
    }
    async handleHelp(ctx) {
        var _a;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        logger_1.logger.info('User requested help', { userId });
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
    async handleStatus(ctx) {
        var _a;
        const userId = (_a = ctx.from) === null || _a === void 0 ? void 0 : _a.id;
        logger_1.logger.info('User requested status', { userId });
        await ctx.reply('✅ Bot is running and ready to process your messages!');
    }
}
exports.CommandHandlers = CommandHandlers;
