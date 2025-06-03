// services/utils/logger.ts
import winston from 'winston';

export const logger = winston.createLogger({
  level: process.env.NODE_ENV === 'production' ? 'info' : 'debug',
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.errors({ stack: true }), // logs stack trace
    winston.format.json()
  ),
  transports: [new winston.transports.Console()],
});
