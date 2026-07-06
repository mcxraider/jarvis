import {
  PostgresUserAuthorizationStore,
  StaticUserAuthorizationStore,
} from '../../../../src/services/telegram/user-authorization.store';

describe('Telegram user authorization stores', () => {
  const telegramIdentity = (id: number) => ({
    telegramId: id,
  });

  it('supports the static store for isolated callers', async () => {
    const store = new StaticUserAuthorizationStore([701122767]);

    await expect(store.isAuthorized(telegramIdentity(701122767))).resolves.toBe(true);
    await expect(store.isAuthorized(telegramIdentity(123456))).resolves.toBe(false);
  });

  it('authorizes active verified identities returned by Postgres', async () => {
    const database = {
      query: jest.fn().mockResolvedValue({ rowCount: 1, rows: [{ id: 'user-id' }] }),
    } as any;
    const store = new PostgresUserAuthorizationStore(
      'postgresql://unused',
      [],
      database,
    );

    await expect(store.isAuthorized(telegramIdentity(701122767))).resolves.toBe(true);
    expect(database.query).toHaveBeenCalledWith(
      expect.stringContaining('identity.telegram_id = $1'),
      [701122767],
    );
  });

  it('denies identities absent from Postgres', async () => {
    const database = {
      query: jest.fn().mockResolvedValue({ rowCount: 0, rows: [] }),
    } as any;
    const store = new PostgresUserAuthorizationStore(
      'postgresql://unused',
      [],
      database,
    );

    await expect(store.isAuthorized(telegramIdentity(123456))).resolves.toBe(false);
  });

  it('applies the emergency deny-list before querying Postgres', async () => {
    const database = {
      query: jest.fn(),
    } as any;
    const store = new PostgresUserAuthorizationStore(
      'postgresql://unused',
      [701122767],
      database,
    );

    await expect(store.isAuthorized(telegramIdentity(701122767))).resolves.toBe(false);
    expect(database.query).not.toHaveBeenCalled();
  });

  it('fails closed when database authorization is unavailable', async () => {
    const database = {
      query: jest.fn().mockRejectedValue(new Error('database unavailable')),
    } as any;
    const store = new PostgresUserAuthorizationStore(
      'postgresql://unused',
      [],
      database,
    );

    await expect(store.isAuthorized(telegramIdentity(701122767))).resolves.toBe(false);
  });
});
