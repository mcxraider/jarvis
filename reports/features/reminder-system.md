# Reminder System

## Summary

Jarvis should support one-time reminders such as:

> Remind me to submit the form in three hours.

The agent interprets the request and stores a durable reminder in Supabase. A scheduled Supabase Edge Function finds due reminders and sends them directly to the user's Telegram chat. Normal reminder delivery does not invoke the LangGraph agent because the reminder text is already known.

This is separate from a future `daily-brief-dispatch` function. Daily briefs are recurring, generated summaries; reminders are individually scheduled messages.

## Goals

- Create, list, update, and cancel reminders through natural-language Jarvis tools.
- Deliver one-time reminders within approximately one minute of their due time.
- Avoid duplicate delivery when dispatchers overlap or retry.
- Store timestamps in UTC while interpreting user input in the user's configured timezone.
- Retain enough delivery state for retries, support, and auditing.

Recurring reminders and AI-generated content at delivery time are out of scope for the first version.

## Architecture

```text
Telegram user
    |
    v
Node webhook -> Python LangGraph agent -> reminder tool -> Supabase reminders
                                                        |
                                                  pg_cron (1 minute)
                                                        |
                                                        v
                                          reminder-dispatch Edge Function
                                                        |
                                                        v
                                               Telegram Bot API
                                                        |
                                                        v
                                                  Telegram user
```

### Responsibility split

| Component | Responsibility |
|---|---|
| LangGraph agent | Understand reminder intent and request missing details |
| Reminder tools | Create, list, update, and cancel user-owned reminders |
| Supabase Postgres | Persist reminders and atomically claim due work |
| `pg_cron` + `pg_net` | Invoke the dispatcher once per minute |
| `reminder-dispatch` Edge Function | Claim due reminders, call Telegram, and record outcomes |
| Telegram Bot API | Deliver the final message |

## Data Model

Add a `reminders` table:

```sql
create type public.reminder_status as enum (
  'scheduled',
  'processing',
  'sent',
  'cancelled',
  'failed'
);

create table public.reminders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  telegram_chat_id bigint not null,
  message text not null check (length(message) between 1 and 4096),
  due_at timestamptz not null,
  timezone text not null,
  status public.reminder_status not null default 'scheduled',
  attempt_count integer not null default 0,
  next_attempt_at timestamptz,
  claimed_at timestamptz,
  sent_at timestamptz,
  telegram_message_id bigint,
  last_error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index reminders_due_idx
  on public.reminders (coalesce(next_attempt_at, due_at))
  where status in ('scheduled', 'processing');

create index reminders_user_idx
  on public.reminders (user_id, due_at desc);
```

`due_at` is always UTC. `timezone` records the IANA timezone used to interpret and display the reminder. `telegram_chat_id` is stored because a Telegram user ID and destination chat ID are not always interchangeable.

RLS should be enabled. User-facing access must be restricted by `user_id`; the dispatcher should use a private database function or tightly scoped server credential. Public clients must never receive the service-role key or Telegram bot token.

## Agent Tools

Register a reminder domain in the existing tool registry:

- `create_reminder(message, due_at, timezone)`
- `list_reminders(status?, from?, to?)`
- `update_reminder(reminder_id, message?, due_at?)`
- `cancel_reminder(reminder_id)`

The runtime context supplies the canonical `user_id`, configured timezone, and Telegram chat ID. These values must not be accepted from model-generated tool arguments.

Creating a reminder is a mutation. It should follow the application's existing risk classification and confirmation policy. Ambiguous requests such as “remind me Friday” should enter the normal clarification flow before a row is created.

Example confirmation:

> I'll remind you to submit the form on 8 July at 3:00 PM (Asia/Singapore).

## Dispatch Flow

1. `pg_cron` invokes `reminder-dispatch` every minute through `pg_net`.
2. The Edge Function authenticates the scheduler request.
3. It calls a private Postgres claim function with a small batch limit.
4. The claim function selects due rows with `FOR UPDATE SKIP LOCKED`, changes them to `processing`, increments `attempt_count`, and returns the claimed rows in one transaction.
5. The function sends each message using Telegram's `sendMessage` endpoint.
6. On success, it records `sent`, `sent_at`, and the Telegram message ID.
7. On a retryable failure, it returns the reminder to `scheduled` with exponential backoff in `next_attempt_at`.
8. On a permanent failure or exhausted retry budget, it marks the reminder `failed`.

The claim operation must also reclaim rows left in `processing` beyond a lease timeout, for example five minutes. This recovers work if an Edge Function stops after claiming but before recording an outcome.

## Delivery Format

The first version should send static text directly:

```text
⏰ Reminder

Submit the certification form.
```

Direct Telegram delivery is faster, cheaper, and more reliable than invoking the full agent. A future reminder type may request fresh context, such as “At 5 PM, summarize tomorrow's calendar.” That should enqueue a separate agent job rather than extending the normal dispatcher.

## Failure and Idempotency Rules

- Use atomic claims to prevent two dispatcher instances from processing the same row concurrently.
- Treat Telegram `429` and transient `5xx` responses as retryable, honoring `retry_after` when present.
- Treat invalid or blocked chat errors as permanent after recording a sanitized error code.
- Cap attempts, initially at five.
- Never log reminder text, bot tokens, or full Telegram responses in infrastructure logs.
- Updating or cancelling a reminder must require both its ID and the current user's canonical `user_id`.

Exactly-once delivery cannot be guaranteed across an external HTTP call and a database update. The design provides at-least-once processing with strong duplicate reduction. A rare duplicate remains possible if Telegram accepts a message and the function terminates before recording success.

## Security

- Store `TELEGRAM_BOT_TOKEN` as a Supabase Edge Function secret.
- Store the scheduled invocation credential in Supabase Vault.
- Require a scheduler-only authorization secret or signed request.
- Keep claiming and status-transition database functions out of exposed schemas where practical.
- Revoke function execution from `PUBLIC`, `anon`, and `authenticated` unless explicitly required.
- Validate message length and allowed status transitions in the database.
- Enable RLS on `reminders`, even if the initial application connects through a privileged database role.

## Observability

Track:

- Number of reminders created, sent, retried, cancelled, and failed
- Dispatch delay: `sent_at - due_at`
- Telegram response class and sanitized error code
- Number of stale processing leases recovered
- Dispatcher invocation duration and claimed batch size

Alert if due scheduled reminders remain unclaimed for more than five minutes or the failure rate rises materially.

## Implementation Sequence

1. Add the `reminders` schema, indexes, RLS policies, and atomic claim function.
2. Add reminder repository methods and agent tool registrations in Python.
3. Add tool tests for ownership, timezone conversion, confirmation, update, and cancellation.
4. Implement and locally test `reminder-dispatch`.
5. Configure Vault secrets and the one-minute `pg_cron` invocation.
6. Test success, overlapping dispatchers, rate limiting, stale leases, retries, cancellation, and cross-user access.
7. Deploy for one user, inspect delivery latency and logs, then enable it for other users.

## Future Extensions

- Recurring reminders with an explicit recurrence rule and computed `next_due_at`
- Snooze and dismiss buttons using Telegram inline callbacks
- Quiet hours and per-user delivery preferences
- Reminder delivery to channels other than Telegram
- Agent-generated reminders that enqueue a durable background agent run
- A unified scheduled-jobs abstraction shared with daily briefs

