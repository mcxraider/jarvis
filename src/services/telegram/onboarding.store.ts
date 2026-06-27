import { Pool } from 'pg';
import { logger } from '../../utils/logger';

export interface OnboardingStore {
  markSeenIfNew(telegramUserId: number): Promise<boolean>;
}

export class MemoryOnboardingStore implements OnboardingStore {
  private readonly seenUserIds = new Set<number>();

  async markSeenIfNew(telegramUserId: number): Promise<boolean> {
    if (this.seenUserIds.has(telegramUserId)) {
      return false;
    }
    this.seenUserIds.add(telegramUserId);
    return true;
  }
}

export class PostgresOnboardingStore implements OnboardingStore {
  private readonly pool: Pool;
  private setupPromise?: Promise<void>;

  constructor(connectionString: string) {
    this.pool = new Pool({ connectionString });
  }

  async markSeenIfNew(telegramUserId: number): Promise<boolean> {
    await this.ensureTable();
    const result = await this.pool.query(
      `
        INSERT INTO telegram_onboarding_seen (telegram_user_id)
        VALUES ($1)
        ON CONFLICT (telegram_user_id) DO NOTHING
        RETURNING telegram_user_id
      `,
      [telegramUserId],
    );
    return result.rowCount !== null && result.rowCount > 0;
  }

  private async ensureTable(): Promise<void> {
    if (!this.setupPromise) {
      this.setupPromise = this.pool.query(`
        CREATE TABLE IF NOT EXISTS telegram_onboarding_seen (
          telegram_user_id BIGINT PRIMARY KEY,
          first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
      `).then(() => undefined);
    }
    return this.setupPromise;
  }
}

export function createOnboardingStore(): OnboardingStore {
  const postgresDsn = process.env.JARVIS_POSTGRES_DSN || process.env.DATABASE_URL;

  if (postgresDsn) {
    logger.info('telegram.onboarding_store.configured', { store: 'postgres' });
    return new PostgresOnboardingStore(postgresDsn);
  }

  logger.info('telegram.onboarding_store.configured', { store: 'memory' });
  return new MemoryOnboardingStore();
}
