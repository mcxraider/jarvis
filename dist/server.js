"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
// src/server.ts — Express setup, webhook registration, graceful shutdown
const express_1 = __importDefault(require("express"));
const logger_1 = require("./utils/logger");
const webhook_controller_1 = require("./controllers/webhook.controller");
const app_1 = require("./app");
const NGROK_URL = process.env.NGROK_URL;
const TELEGRAM_SECRET_TOKEN = process.env.TELEGRAM_SECRET_TOKEN;
// Register webhook with Telegram (runs once per URL; safe to call on every start)
(async () => {
    try {
        await app_1.botService.setupWebhook(NGROK_URL, TELEGRAM_SECRET_TOKEN);
    }
    catch (err) {
        logger_1.logger.error('Error setting up webhook:', err);
    }
})();
// Express app
const app = (0, express_1.default)();
app.use(express_1.default.json());
app.get('/ping', (_req, res, _next) => {
    res.json({ status: 'ok' });
});
app.use((0, webhook_controller_1.createWebhookRouter)(app_1.botService));
// Start listening
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    logger_1.logger.info(`Server started at http://localhost:${PORT}`);
    logger_1.logger.info(`Waiting for Telegram updates on /webhook/:secret`);
});
// Graceful shutdown
process.on('SIGTERM', () => {
    logger_1.logger.info('Shutting down gracefully...');
    process.exit(0);
});
