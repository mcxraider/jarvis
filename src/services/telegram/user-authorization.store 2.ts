import { Pool } from 'pg';
import { logger } from '../../utils/logger';

export interface TelegramIdentityReference {
  telegramId: number;
}

export interface UserAuthorizationStore {
  isAuthorized(identity: TelegramIdentityReference): Promise<boolean>;
}

type Queryable = Pick<Pool, 'query'>;

export class StaticUserAuthorizationStore implements UserAuthorizationStore {
  private readonly allowedUserIds: Set<number>;

  constructor(allowedUserIds: Iterable<number>) {
    this.allowedUserIds = new Set(allowedUserIds);
  }

  async isAuthorized(identity: TelegramIdentityReference): Promise<boolean> {
    return this.allowedUserIds.has(identity.telegramId);
  }
}

export class PostgresUserAuthorizationStore implements UserAuthorizationStore {
  private readonly deniedUserIds: Set<number>;

  constructor(
    connectionString: string,
    deniedUserIds: Iterable<number> = [],
    private readonly database: Queryable = new Pool({ connectionString }),
  ) {
    this.deniedUserIds = new Set(deniedUserIds);
  }

  async isAuthorized(identityReference: TelegramIdentityReference): Promise<boolean> {
    if (this.deniedUserIds.has(identityReference.telegramId)) {
      return false;
    }

    try {
      const result = await this.database.query(
        `
          UPDATE public.telegram_identities AS identity
          SET last_seen_at = NOW()
          FROM public.users AS app_user
          WHERE identity.user_id = app_user.id
            AND identity.telegram_id = $1
            AND identity.verified_at IS NOT NULL
            AND app_user.status = 'active'
          RETURNING app_user.id
        `,
        [identityReference.telegramId],
      );
      return result.rowCount === 1;
    } catch (error) {
      logger.error('telegram.authorization.database_failed', {
        error: error instanceof Error ? error.message : String(error),
      });
      return false;
    }
  }
}

function parseTelegramIdSet(rawValue?: string): Set<number> {
  if (!rawValue?.trim()) {
    return new Set();
  }

  const values = rawValue
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
    .map(Number);

  if (values.some((value) => !Number.isSafeInteger(value) || value <= 0)) {
    throw new Error('TELEGRAM_EMERGENCY_DENY_USER_IDS must contain positive numeric IDs');
  }

  return new Set(values);
}

export function createUserAuthorizationStore(): UserAuthorizationStore {
  const connectionString =
    process.env.JARVIS_POSTGRES_DSN ||
    process.env.DATABASE_URL;

  if (!connectionString) {
    throw new Error(
      'Database-backed Telegram authorization requires JARVIS_POSTGRES_DSN or DATABASE_URL',
    );
  }

  const deniedUserIds = parseTelegramIdSet(
    process.env.TELEGRAM_EMERGENCY_DENY_USER_IDS,
  );
  logger.info('telegram.authorization.configured', {
    store: 'postgres',
    emergencyDeniedUsers: deniedUserIds.size,
  });
  return new PostgresUserAuthorizationStore(connectionString, deniedUserIds);
}
