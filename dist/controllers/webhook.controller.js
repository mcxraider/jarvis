"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.createWebhookRouter = createWebhookRouter;
// services/WebhookController.ts
const express_1 = __importDefault(require("express"));
const logger_1 = require("../utils/logger");
function createWebhookRouter(botService) {
    const router = express_1.default.Router();
    router.post('/webhook/:secret', express_1.default.json(), async (req, res) => {
        const secret = req.params.secret;
        if (!secret || secret !== process.env.TELEGRAM_SECRET_TOKEN) {
            logger_1.logger.warn('Unauthorized webhook attempt', { ip: req.ip, secret });
            res.sendStatus(401);
            return;
        }
        try {
            await botService.handleUpdate(req.body);
            res.sendStatus(200);
            return;
        }
        catch (err) {
            logger_1.logger.error('Webhook handler failed', { error: err.message, stack: err.stack });
            res.status(500).json({ error: 'Internal Server Error' });
            return;
        }
    });
    return router;
}
