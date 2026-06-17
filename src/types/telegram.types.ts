export interface TelegramConfig {
  token: string;
  allowedUserIds: number[];
  webhookUrl?: string;
  secretToken?: string;
  maxConnections?: number;
}
