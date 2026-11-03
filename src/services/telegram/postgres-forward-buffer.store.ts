import { Pool } from 'pg';
import {
  AcknowledgeResult,
  ForwardBufferStore,
  ForwardedMessage,
  PushResult,
} from './forward-buffer.store';

interface Entry {
  messages: ForwardedMessage[];
  confirmation_message_id?: number;
}

export class PostgresForwardBufferStore implements ForwardBufferStore {
  constructor(
    private readonly pool: Pool,
    private readonly botId: string,
  ) {}

  private async modify<T>(key: string, fn: (entry: Entry) => T): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [
        `forward:${this.botId}:${key}`,
      ]);
      const result = await client.query(
        "SELECT messages,confirmation_message_id FROM private.telegram_forward_buffers WHERE bot_id=$1 AND conversation_key=$2 AND updated_at>now()-interval '1 hour' FOR UPDATE",
        [this.botId, key],
      );
      const row = result.rows[0];
      const entry: Entry = {
        messages: (row?.messages ?? []).map((message: ForwardedMessage) => ({
          ...message,
          forwardedAt: new Date(message.forwardedAt),
          receivedAt: new Date(message.receivedAt),
        })),
        confirmation_message_id:
          row?.confirmation_message_id == null ? undefined : Number(row.confirmation_message_id),
      };
      const before = JSON.stringify(entry);
      const value = fn(entry);
      if (JSON.stringify(entry) === before) {
        await client.query('COMMIT');
        return value;
      }
      if (entry.messages.length) {
        await client.query(
          `INSERT INTO private.telegram_forward_buffers (bot_id,conversation_key,messages,confirmation_message_id)
          VALUES ($1,$2,$3,$4) ON CONFLICT (bot_id,conversation_key) DO UPDATE SET messages=excluded.messages,
          confirmation_message_id=excluded.confirmation_message_id,updated_at=now()`,
          [this.botId, key, JSON.stringify(entry.messages), entry.confirmation_message_id ?? null],
        );
      } else {
        await client.query(
          'DELETE FROM private.telegram_forward_buffers WHERE bot_id=$1 AND conversation_key=$2',
          [this.botId, key],
        );
      }
      await client.query('COMMIT');
      return value;
    } catch (error) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw error;
    } finally {
      client.release();
    }
  }

  push(key: string, message: ForwardedMessage): Promise<PushResult> {
    return this.modify(key, (entry) => {
      if (message.id && entry.messages.some((item) => item.id === message.id))
        return { ok: true, count: entry.messages.length };
      if (message.audio && entry.messages.some((item) => item.audio))
        return { ok: false, reason: 'audio_already_buffered' };
      if (message.text.length > 4096) return { ok: false, reason: 'message_too_long' };
      if (
        entry.messages.length >= 50 ||
        entry.messages.reduce((n, m) => n + m.text.length, message.text.length) > 32768
      )
        return { ok: false, reason: 'buffer_full' };
      entry.messages.push(message);
      return { ok: true, count: entry.messages.length };
    });
  }
  peek(key: string): Promise<ForwardedMessage[]> {
    return this.modify(key, (entry) => entry.messages.slice());
  }
  count(key: string): Promise<number> {
    return this.modify(key, (entry) => entry.messages.length);
  }
  getConfirmationMessageId(key: string): Promise<number | undefined> {
    return this.modify(key, (entry) => entry.confirmation_message_id);
  }
  setConfirmationMessageId(key: string, id: number): Promise<void> {
    return this.modify(key, (entry) => {
      entry.confirmation_message_id = id;
    });
  }
  clear(key: string): Promise<void> {
    return this.modify(key, (entry) => {
      entry.messages = [];
    });
  }
  acknowledge(key: string, prefix: ForwardedMessage[]): Promise<AcknowledgeResult> {
    return this.modify(key, (entry) => {
      if (!prefix.length) return { acknowledged: false, reason: 'empty' };
      if (
        prefix.length > entry.messages.length ||
        prefix.some((message, i) => !message.id || message.id !== entry.messages[i].id)
      )
        return { acknowledged: false, reason: 'mismatch' };
      entry.messages.splice(0, prefix.length);
      return {
        acknowledged: true,
        remainingCount: entry.messages.length,
        confirmationMessageId: entry.confirmation_message_id,
      };
    });
  }
}
