import { buildConversationKey, hashIdentifier, mapTelegramUserId } from '../../../../src/services/telegram/conversation-key';

describe('buildConversationKey', () => {
  it('returns telegram-chat:{hash} when chatId is provided', () => {
    const key = buildConversationKey(12345, 'jerry', 99999);
    expect(key).toMatch(/^telegram-chat:[a-f0-9]{32}$/);
  });

  it('returns telegram:{hash} when only telegramUserId', () => {
    const key = buildConversationKey(12345, 'jerry', undefined);
    expect(key).toMatch(/^telegram:[a-f0-9]{32}$/);
  });

  it('returns internal:{id} for anonymous/internal users', () => {
    const key = buildConversationKey(undefined, 'internal-user-1', undefined);
    expect(key).toBe('internal:internal-user-1');
  });

  it('same inputs always produce same key (deterministic)', () => {
    const key1 = buildConversationKey(12345, 'jerry', 99999);
    const key2 = buildConversationKey(12345, 'jerry', 99999);
    expect(key1).toBe(key2);
  });

  it('different chatId+user combos produce different keys', () => {
    const key1 = buildConversationKey(12345, 'jerry', 11111);
    const key2 = buildConversationKey(12345, 'jerry', 22222);
    expect(key1).not.toBe(key2);
  });

  it('uses telegramUserId over internalUserId when chatId present', () => {
    const withTgId = buildConversationKey(12345, 'jerry', 99999);
    const withoutTgId = buildConversationKey(undefined, 'jerry', 99999);
    expect(withTgId).not.toBe(withoutTgId);
  });
});

describe('hashIdentifier', () => {
  it('returns 32-char hex string', () => {
    const hash = hashIdentifier(12345);
    expect(hash).toMatch(/^[a-f0-9]{32}$/);
  });

  it('number and string of same value produce same hash', () => {
    expect(hashIdentifier(12345)).toBe(hashIdentifier('12345'));
  });
});

describe('mapTelegramUserId', () => {
  afterEach(() => {
    delete process.env.TELEGRAM_USER_MAP;
  });

  it('returns anonymous for undefined', () => {
    expect(mapTelegramUserId(undefined)).toBe('anonymous');
  });

  it('returns telegram:{id} fallback when no map configured', () => {
    expect(mapTelegramUserId(12345)).toBe('telegram:12345');
  });

  it('returns mapped name when TELEGRAM_USER_MAP matches', () => {
    process.env.TELEGRAM_USER_MAP = '12345:jerry,67890:alex';
    expect(mapTelegramUserId(12345)).toBe('jerry');
    expect(mapTelegramUserId(67890)).toBe('alex');
  });

  it('returns telegram:{id} when map exists but no match', () => {
    process.env.TELEGRAM_USER_MAP = '99999:bob';
    expect(mapTelegramUserId(12345)).toBe('telegram:12345');
  });

  it('handles whitespace in map entries', () => {
    process.env.TELEGRAM_USER_MAP = ' 12345 : jerry , 67890 : alex ';
    expect(mapTelegramUserId(12345)).toBe('jerry');
  });
});
