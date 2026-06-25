import { buildTelegramThreadId } from '../../../../src/services/telegram/telegram-thread-id';

describe('buildTelegramThreadId', () => {
  it('returns a stable per-user thread id', () => {
    expect(buildTelegramThreadId(12345, 'telegram:12345')).toBe(
      buildTelegramThreadId(12345, 'telegram:12345'),
    );
  });

  it('does not vary by caller-provided mapped internal user id when Telegram ID exists', () => {
    expect(buildTelegramThreadId(12345, 'jerry')).toBe(
      buildTelegramThreadId(12345, 'telegram:12345'),
    );
  });

  it('returns different thread ids for different Telegram users', () => {
    expect(buildTelegramThreadId(12345, 'telegram:12345')).not.toBe(
      buildTelegramThreadId(67890, 'telegram:67890'),
    );
  });

  it('falls back to internal user id when Telegram ID is unavailable', () => {
    expect(buildTelegramThreadId(undefined, 'api-user')).toMatch(/^tg_user_[a-f0-9]{32}$/);
  });
});
