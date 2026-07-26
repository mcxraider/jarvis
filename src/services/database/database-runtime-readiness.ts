import { Pool } from 'pg';

const REQUIRED_TABLES = [
  'public.users',
  'public.telegram_identities',
  'public.user_preferences',
  'public.telegram_pending_clarifications',
  'public.telegram_conversation_gates',
  'public.rate_limits',
] as const;

export interface DatabaseRuntimeReadiness {
  role: string;
  inheritedRole: string;
  tables: readonly string[];
}

export async function verifyDatabaseRuntime(
  connectionString: string,
): Promise<DatabaseRuntimeReadiness> {
  const TIMEOUT_MS = 15_000;
  const pool = new Pool({ connectionString, max: 1, connectionTimeoutMillis: 10_000 });

  const work = async (): Promise<DatabaseRuntimeReadiness> => {
    const roleResult = await pool.query<{
      current_user: string;
      inherits_runtime: boolean;
    }>(
      `
        SELECT current_user,
               pg_has_role(current_user, 'jarvis_runtime', 'MEMBER') AS inherits_runtime
      `,
    );
    const role = roleResult.rows[0]?.current_user;
    if (role !== 'jarvis_app') {
      throw new Error(
        `Database runtime must connect as jarvis_app; connected as ${role ?? 'unknown'}`,
      );
    }
    if (!roleResult.rows[0]?.inherits_runtime) {
      throw new Error('Database role jarvis_app must inherit jarvis_runtime');
    }

    const tableResult = await pool.query<{ table_name: string; relation: string | null }>(
      `
        SELECT table_name, to_regclass(table_name) AS relation
        FROM unnest($1::text[]) AS required(table_name)
      `,
      [REQUIRED_TABLES],
    );
    const missingTables = tableResult.rows
      .filter((row) => row.relation === null)
      .map((row) => row.table_name);
    if (missingTables.length > 0) {
      throw new Error(
        `Database migrations are incomplete; missing: ${missingTables.join(', ')}`,
      );
    }

    await pool.query(`
      SELECT 1
      FROM public.users
      JOIN public.telegram_identities ON public.telegram_identities.user_id = public.users.id
      LEFT JOIN public.user_preferences ON public.user_preferences.user_id = public.users.id
      LIMIT 0
    `);
    // Select every migration-gated runtime column explicitly. A bare SELECT 1 only
    // proves the table exists and lets a partially migrated deployment start before
    // its first real Telegram request fails.
    await pool.query(`
      SELECT pending_key, request_id, clarification_message_id, prompt_message_id
      FROM public.telegram_pending_clarifications
      LIMIT 0
    `);
    await pool.query(`
      SELECT gate_key, active_request_id
      FROM public.telegram_conversation_gates
      LIMIT 0
    `);
    await pool.query('SELECT 1 FROM public.rate_limits LIMIT 0');

    return {
      role,
      inheritedRole: 'jarvis_runtime',
      tables: REQUIRED_TABLES,
    };
  };

  try {
    return await Promise.race([
      work(),
      new Promise<never>((_, reject) =>
        setTimeout(
          () => reject(new Error('Database readiness check timed out after 15s')),
          TIMEOUT_MS,
        ),
      ),
    ]);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `Database runtime readiness failed: ${detail}. Apply Supabase migrations and verify JARVIS_POSTGRES_DSN uses jarvis_app.`,
    );
  } finally {
    await pool.end().catch(() => {});
  }
}
