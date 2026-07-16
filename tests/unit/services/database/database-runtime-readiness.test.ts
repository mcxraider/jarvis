import { Pool } from 'pg';
import { verifyDatabaseRuntime } from '../../../../src/services/database/database-runtime-readiness';

jest.mock('pg', () => ({ Pool: jest.fn() }));

const requiredTables = [
  'public.users',
  'public.telegram_identities',
  'public.user_preferences',
  'public.telegram_pending_clarifications',
  'public.telegram_conversation_gates',
  'public.rate_limits',
];

function installPool(query: jest.Mock): jest.Mock {
  const end = jest.fn().mockResolvedValue(undefined);
  (Pool as unknown as jest.Mock).mockImplementation(() => ({ query, end }));
  return end;
}

describe('verifyDatabaseRuntime', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('checks the Telegram generation and prompt columns before reporting readiness', async () => {
    const query = jest.fn().mockImplementation(async (sql: string) => {
      if (sql.includes('pg_has_role')) {
        return { rows: [{ current_user: 'jarvis_app', inherits_runtime: true }] };
      }
      if (sql.includes('to_regclass')) {
        return {
          rows: requiredTables.map((table_name) => ({ table_name, relation: table_name })),
        };
      }
      return { rows: [] };
    });
    const end = installPool(query);

    await expect(verifyDatabaseRuntime('postgres://runtime')).resolves.toMatchObject({
      role: 'jarvis_app',
      inheritedRole: 'jarvis_runtime',
    });

    const sql = query.mock.calls.map(([statement]) => statement).join('\n');
    expect(sql).toContain('active_request_id');
    expect(sql).toContain('clarification_message_id');
    expect(sql).toContain('prompt_message_id');
    expect(end).toHaveBeenCalledTimes(1);
  });

  it('fails startup when a required Telegram prompt column is not migrated', async () => {
    const query = jest.fn().mockImplementation(async (sql: string) => {
      if (sql.includes('pg_has_role')) {
        return { rows: [{ current_user: 'jarvis_app', inherits_runtime: true }] };
      }
      if (sql.includes('to_regclass')) {
        return {
          rows: requiredTables.map((table_name) => ({ table_name, relation: table_name })),
        };
      }
      if (sql.includes('clarification_message_id')) {
        throw new Error('column "clarification_message_id" does not exist');
      }
      return { rows: [] };
    });
    const end = installPool(query);

    await expect(verifyDatabaseRuntime('postgres://runtime')).rejects.toThrow(
      'Database runtime readiness failed: column "clarification_message_id" does not exist',
    );
    expect(end).toHaveBeenCalledTimes(1);
  });
});
