import { Pool } from 'pg';
import { logger } from '../../utils/logger';

const pools = new Map<string, Pool>();

export function positiveInteger(name: string, fallback: number): number {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isSafeInteger(value) || value < 1)
    throw new Error(`${name} must be a positive integer`);
  return value;
}

/** All Node runtime stores using the same DSN share one bounded pool. */
export function getRuntimePool(connectionString: string): Pool {
  let pool = pools.get(connectionString);
  if (!pool) {
    pool = new Pool({
      connectionString,
      max: positiveInteger('JARVIS_NODE_DB_POOL_MAX', 6),
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 5_000,
      query_timeout: 5_000,
    });
    pool.on('error', () => logger.warn('database.pool.connection_error'));
    pools.set(connectionString, pool);
  }
  return pool;
}

export async function closeRuntimePools(): Promise<void> {
  const current = [...pools.values()];
  pools.clear();
  await Promise.all(current.map((pool) => pool.end()));
}
