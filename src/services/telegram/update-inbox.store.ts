import { randomUUID } from 'crypto';
import { Pool } from 'pg';
import { buildConversationKey, mapTelegramUserId } from './conversation-key';
import { DispatchRecord } from './work-context';

export interface InboxMessage {
  message_id?: number;
  from?: { id: number };
  chat?: { id: number };
  text?: string;
  media_group_id?: string;
  photo?: Array<{ file_id: string; width: number; height: number }>;
  voice?: { file_id: string };
  audio?: { file_id: string };
  document?: { mime_type?: string };
  forward_origin?: unknown;
  forward_date?: number;
}
export interface InboxUpdate {
  update_id: number;
  message?: InboxMessage;
  edited_message?: InboxMessage;
  callback_query?: { from?: { id: number }; message?: InboxMessage };
}

export interface InboxJob {
  ids: string[];
  payloads: InboxUpdate[];
  owner: string;
  stage: 'received' | 'dispatched';
  dispatch?: DispatchRecord;
  attempts: number;
  createdAt: Date;
}

export class PostgresUpdateInbox {
  constructor(
    private readonly pool: Pool,
    readonly botId: string,
    private readonly capacity = 100,
  ) {}

  async enqueue(update: InboxUpdate): Promise<boolean> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      // Serialize capacity checks with inserts (duplicates remain successful at capacity).
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [
        `jarvis-inbox:${this.botId}`,
      ]);
      const existing = await client.query(
        'SELECT 1 FROM private.telegram_update_inbox WHERE bot_id=$1 AND update_id=$2',
        [this.botId, update.update_id],
      );
      if (existing.rowCount) {
        await client.query('COMMIT');
        return true;
      }
      const count = await client.query(
        "SELECT count(*)::int AS count FROM private.telegram_update_inbox WHERE bot_id=$1 AND state IN ('pending','processing')",
        [this.botId],
      );
      if (count.rows[0].count >= this.capacity) {
        await client.query('ROLLBACK');
        return false;
      }
      const message = update.message ?? update.callback_query?.message;
      const sender =
        update.message?.from ?? update.callback_query?.from ?? update.edited_message?.from;
      const key = buildConversationKey(
        sender?.id,
        mapTelegramUserId(sender?.id),
        message?.chat?.id,
      );
      const isForward = Boolean(message?.forward_origin || message?.forward_date);
      const control =
        !update.callback_query &&
        !isForward &&
        /^\/(cancel|status|help|start)(?:@\w+)?(?:\s|$)/.test(message?.text ?? '');
      const album =
        !isForward && message?.photo && message?.media_group_id
          ? `${key}:${message.media_group_id}`
          : null;
      if (album) {
        // Existing pending siblings share the quiet deadline; processing albums are immutable.
        await client.query(
          "UPDATE private.telegram_update_inbox SET available_at=now()+interval '1500 milliseconds' WHERE bot_id=$1 AND album_key=$2 AND state='pending'",
          [this.botId, album],
        );
      }
      await client.query(
        `INSERT INTO private.telegram_update_inbox
        (bot_id,update_id,conversation_key,album_key,is_control,payload,available_at)
        VALUES ($1,$2,$3,$4,$5,$6,now()+CASE WHEN $4::text IS NULL THEN interval '0 seconds' ELSE interval '1500 milliseconds' END)`,
        [this.botId, update.update_id, key, album, control, update],
      );
      await client.query('COMMIT');
      return true;
    } catch (error) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  async claim(
    control: boolean,
    allowAudio = true,
    allowImages = true,
  ): Promise<InboxJob | undefined> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [
        `jarvis-claim:${this.botId}`,
      ]);
      const selected = await client.query(
        `SELECT * FROM private.telegram_update_inbox candidate
        WHERE bot_id=$1 AND is_control=$2 AND available_at<=now()
          AND ($3 OR NOT (coalesce(payload->'message','{}'::jsonb) ?| array['voice','audio']) AND coalesce(payload->'message'->'document'->>'mime_type','') NOT LIKE 'audio/%')
          AND ($4 OR NOT (coalesce(payload->'message','{}'::jsonb) ? 'photo')) AND
          (state='pending' OR (state='processing' AND lease_expires_at<now()))
          AND ($2 OR NOT EXISTS (SELECT 1 FROM private.telegram_update_inbox live
            WHERE live.bot_id=candidate.bot_id AND live.conversation_key=candidate.conversation_key
            AND NOT live.is_control AND live.state='processing' AND live.lease_expires_at>now()))
          AND ($2 OR NOT EXISTS (SELECT 1 FROM private.telegram_update_inbox earlier
            WHERE earlier.bot_id=candidate.bot_id AND earlier.conversation_key=candidate.conversation_key
            AND NOT earlier.is_control AND earlier.state IN ('pending','processing')
            AND earlier.update_id<candidate.update_id
            AND (candidate.album_key IS NULL OR earlier.album_key IS DISTINCT FROM candidate.album_key)))
        ORDER BY update_id LIMIT 1 FOR UPDATE SKIP LOCKED`,
        [this.botId, control, allowAudio, allowImages],
      );
      const first = selected.rows[0];
      if (!first) {
        await client.query('COMMIT');
        return;
      }
      const owner = randomUUID();
      // At most ten members. An already dispatched group keeps its original lease membership.
      const siblings =
        first.album_key && first.stage === 'received'
          ? await client.query(
              `SELECT * FROM private.telegram_update_inbox WHERE bot_id=$1 AND album_key=$2
            AND (state='pending' OR (state='processing' AND lease_expires_at<now()))
            ORDER BY update_id LIMIT 10 FOR UPDATE`,
              [this.botId, first.album_key],
            )
          : first.owner_token
            ? await client.query(
                'SELECT * FROM private.telegram_update_inbox WHERE bot_id=$1 AND owner_token=$2 ORDER BY update_id FOR UPDATE',
                [this.botId, first.owner_token],
              )
            : { rows: [first] };
      const ids = siblings.rows.map((row) => String(row.update_id));
      await client.query(
        `UPDATE private.telegram_update_inbox SET state='processing',owner_token=$3,
        lease_expires_at=now()+interval '30 seconds',attempts=attempts+1 WHERE bot_id=$1 AND update_id=ANY($2::bigint[])`,
        [this.botId, ids, owner],
      );
      await client.query('COMMIT');
      return {
        ids,
        payloads: siblings.rows.map((row) => row.payload),
        owner,
        stage: first.stage,
        dispatch: first.dispatch ?? undefined,
        attempts: first.attempts + 1,
        createdAt: new Date(first.created_at),
      };
    } catch (error) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  async renew(job: InboxJob): Promise<boolean> {
    const result = await this.pool.query(
      `UPDATE private.telegram_update_inbox SET lease_expires_at=now()+interval '30 seconds'
      WHERE bot_id=$1 AND owner_token=$2 AND state='processing' AND lease_expires_at>now()`,
      [this.botId, job.owner],
    );
    return result.rowCount === job.ids.length;
  }

  async dispatched(job: InboxJob, record: DispatchRecord): Promise<void> {
    const result = await this.pool.query(
      `UPDATE private.telegram_update_inbox SET stage='dispatched',dispatch=$3
      WHERE bot_id=$1 AND owner_token=$2 AND state='processing' AND lease_expires_at>now()`,
      [this.botId, job.owner, record],
    );
    if (result.rowCount !== job.ids.length) throw new Error('Inbox lease lost before dispatch');
    job.stage = 'dispatched';
    job.dispatch = record;
  }

  async settle(
    job: InboxJob,
    state: 'completed' | 'failed' | 'reconciliation' | 'pending',
  ): Promise<void> {
    const result = await this.pool.query(
      `UPDATE private.telegram_update_inbox SET state=$3,
      finished_at=CASE WHEN $3='pending' THEN NULL ELSE now() END,
      available_at=now()+interval '5 seconds',owner_token=NULL,lease_expires_at=NULL
      WHERE bot_id=$1 AND owner_token=$2 AND state='processing' AND lease_expires_at>now()`,
      [this.botId, job.owner, state],
    );
    if (result.rowCount !== job.ids.length) throw new Error('Inbox lease lost before settlement');
  }

  async ready(): Promise<void> {
    await this.pool.query(
      'SELECT update_id,state,stage,dispatch,owner_token,lease_expires_at FROM private.telegram_update_inbox LIMIT 0',
    );
    await this.pool.query(
      'SELECT messages,confirmation_message_id,updated_at FROM private.telegram_forward_buffers LIMIT 0',
    );
  }

  async cleanup(): Promise<void> {
    await this.pool.query(
      "UPDATE private.telegram_update_inbox SET payload=NULL WHERE state NOT IN ('pending','processing') AND finished_at<now()-interval '24 hours' AND payload IS NOT NULL",
    );
    await this.pool.query(
      "DELETE FROM private.telegram_update_inbox WHERE state IN ('completed','failed') AND finished_at<now()-interval '7 days'",
    );
    await this.pool.query(
      "DELETE FROM private.telegram_forward_buffers WHERE updated_at<now()-interval '1 hour'",
    );
  }
}
