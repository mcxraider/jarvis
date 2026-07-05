# Reminder System — Design and Implementation Plan

## Summary

Jarvis should support one-time reminders such as:

> Remind me to submit the form in three hours.

The agent interprets the request during a normal conversation turn and stores a durable
reminder row in Supabase Postgres. A scheduled dispatcher (Supabase `pg_cron` invoking a
`reminder-dispatch` Edge Function through `pg_net`) finds due reminders once per minute,
claims them atomically, and sends them directly to the user's Telegram chat via the Bot
API. Normal reminder delivery never invokes the LangGraph agent, because the reminder
text was fully determined at creation time — delivery is a dumb, cheap, reliable pipe.

This is deliberately separate from a future `daily-brief-dispatch` function. Daily briefs
are recurring, generated summaries that need fresh context and an LLM call at delivery
time; reminders are individually scheduled, pre-written messages. Sharing a dispatcher
would couple a latency-sensitive static path to an expensive generated path. A shared
"scheduled jobs" abstraction is listed under Future Extensions, but the first version
should not build it.

## Goals

- Create, list, update, and cancel reminders through natural-language Jarvis tools.
- Deliver one-time reminders within approximately one minute of their due time.
- Avoid duplicate delivery when dispatcher runs overlap, crash, or retry.
- Store timestamps in UTC while interpreting user input in the user's configured timezone.
- Retain enough delivery state for retries, support, and auditing.
- Survive restarts of both the Node service and the Python agent API: once a reminder row
  exists, delivery depends only on Supabase infrastructure and Telegram.

### Non-goals (first version)

- Recurring reminders (`every Monday at 9`). The data model leaves room for this but no
  recurrence engine is built.
- AI-generated content at delivery time ("at 5 PM summarize tomorrow's calendar"). This
  requires a durable agent-run queue, which is a different feature.
- Snooze / dismiss inline buttons. Delivery is a plain message.
- Non-Telegram delivery channels.
- Cross-user reminders ("remind Zachary to…"). Every reminder is owned by and delivered
  to the user who created it.

## Where This Fits in the Existing Codebase

The design should reuse existing seams rather than invent parallel ones:

| Existing piece | Role in the reminder system |
|---|---|
| `agents/agent_api/app/tools/` (ToolSpec registry, `base.py`) | Reminder tools are registered as a new tool domain with specs, handlers, and LangChain wrappers, exactly like Todoist and Calendar. |
| `agents/agent_api/app/tools/domain_adapters.py` | Registration point for domains — but see the note below: reminders are a *first-party* domain with no external credential, so the adapter contract needs a small extension. |
| `agents/agent_api/app/user_context/runtime.py` (`RuntimeContextSnapshot`) | Supplies the canonical `user_id`, `timezone`, and locale. Tool handlers must take these from the snapshot, never from model-generated arguments. |
| `agents/agent_api/app/graph/risk.py` + `tools/metadata.py` | Risk classification for the confirm gate. Reminder mutations slot into the existing "single reversible mutation = low, bulk or always-risky = confirm" policy. |
| `public.user_identities` + `resolve_user_id` (migration `20260704140344`) | Source of truth for mapping Telegram identity to canonical `user_id`, and the place to read the Telegram chat ID at dispatch time. |
| `jarvis_runtime` role + `private` schema (migration `20260704140023`) | The agent API's constrained database role. Reminder CRUD is granted to it; dispatcher-only claim functions live in `private` and are *not* granted to it. |
| `supabase/migrations/` naming convention | New DDL ships as timestamped migrations, same as the existing ones. |
| `src/utils/logger.ts` conventions | The Edge Function is Deno, not the Node service, but its logs should follow the same event-name style (`reminder.dispatch.claimed`, `reminder.dispatch.sent`) and the same redaction rules. |

### First-party domain note

`DomainAdapter` currently assumes an `IntegrationCredential` resolved from an enabled
integration connection — its `build_client` takes a credential, and domain availability
is derived from `integration_connections`. Reminders have no external credential: the
"client" is the agent API's own Postgres connection. Two reasonable options:

1. **Extend the adapter contract** with an optional `requires_credential: bool = True`
   flag (or a `first_party` variant). When false, the runtime activates the domain
   unconditionally for every resolved user and `build_client` receives the database
   handle instead of a credential. This keeps one registry and one activation path.
2. **Register reminder tools outside `DOMAIN_ADAPTERS`**, directly in the graph
   builder's tool assembly.

Option 1 is preferred. It preserves the invariant stated in `domain_adapters.py` — one
registration point per domain, no edits to `builder.py` — and it means the reminder
domain automatically participates in `RuntimeContextSnapshot.domains`, prompt-fragment
assembly, and tool-name registration like every other domain. Option 2 creates a second
activation path that every future first-party domain (daily briefs, preferences editing)
would also want, so the contract extension pays for itself immediately.

## Architecture

```text
Telegram user
    |
    v
Node webhook (src/services/telegram/) 
    |
    v
Python LangGraph agent (agents/agent_api/)
    |
    v
reminder tool handlers  ---- insert/update/select ---->  Supabase Postgres (public.reminders)
                                                              |
                                                        pg_cron (every minute)
                                                              |
                                                          pg_net HTTP POST
                                                              |
                                                              v
                                              reminder-dispatch Edge Function (Deno)
                                                              |
                                          claim via private.claim_due_reminders(...)
                                                              |
                                                              v
                                                     Telegram Bot API sendMessage
                                                              |
                                                              v
                                                        Telegram user
```

### Responsibility split

| Component | Responsibility |
|---|---|
| LangGraph agent | Understand reminder intent, resolve relative times against the user's timezone, ask for missing details via the normal clarification flow |
| Reminder tool handlers (Python) | Validate arguments, enforce ownership from runtime context, perform CRUD against `public.reminders` through the `jarvis_runtime` role |
| Supabase Postgres | Persist reminders, enforce constraints and status transitions, atomically claim due work |
| `pg_cron` + `pg_net` | Invoke the dispatcher once per minute with a shared-secret header |
| `reminder-dispatch` Edge Function | Authenticate the scheduler call, claim a batch, call Telegram, record outcomes, schedule retries |
| Telegram Bot API | Deliver the final message |

### Why these choices

**Why Supabase-side dispatch instead of a Node `setInterval` or Python scheduler.** The
Node service and agent API are stateless request handlers today; neither has a durable
job runner, and both restart on deploys. Putting the schedule in `pg_cron` and the
delivery in an Edge Function means reminders fire even when the app processes are down
or mid-deploy. It also keeps the trigger and the data in the same failure domain — if
Postgres is down, there is nothing to dispatch anyway.

**Why the dispatcher calls Telegram directly instead of going through the Node
service.** Routing delivery back through the Node service would add a hop, require an
authenticated internal endpoint, and couple delivery availability to app uptime — for a
message whose text is already final. The Edge Function holds the bot token as a secret
and calls `sendMessage` itself. The cost is a second place that knows the bot token and
a second implementation of Telegram error handling, which is acceptable for one endpoint
and documented failure rules.

**Why polling every minute instead of per-reminder scheduling.** One-minute granularity
matches the product promise ("within approximately one minute") and a single cron entry
is dramatically simpler to operate than one `pg_cron` job per reminder or LISTEN/NOTIFY
plumbing. A minutely poll over an indexed partial scan of due rows is effectively free
at this scale (single-digit users).

**Why the claim function lives in Postgres rather than the Edge Function doing
`SELECT` + `UPDATE`.** The claim must be atomic under concurrency (overlapping cron
fires, manual invocations, redeploys). `FOR UPDATE SKIP LOCKED` inside one SQL function
gives that for free; doing it in two round-trips from Deno reintroduces the race.

## Data Model

Add a `reminders` table via a new timestamped migration:

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
  original_input text,
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

### Column-by-column rationale

- **`user_id`** — canonical UUID from `public.users`, resolved by the runtime context.
  Cascading delete keeps orphan reminders from surviving account removal.
- **`telegram_chat_id`** — captured at creation time from the runtime context, because a
  Telegram *user* ID and the destination *chat* ID are not always interchangeable, and
  because the dispatcher must not need a join against identity tables on the hot path.
  Trade-off: if the user's chat migrates, stale rows point at the old chat. Acceptable
  for a single-user-per-chat bot; note it as a known limitation.
- **`message`** — the final delivery text, fully rendered at creation. The 4096 cap is
  Telegram's message limit; enforcing it in the database means the dispatcher can never
  be handed an unsendable row.
- **`due_at`** — always UTC (`timestamptz`). All interpretation of user phrasing happens
  in the tool handler at creation time; the dispatcher only ever compares against
  `now()`.
- **`timezone`** — the IANA timezone that was used to interpret the request, snapshotted
  per row. This matters because the user's *preference* can change later; the reminder
  should still display and be reasoned about in the timezone it was created under.
- **`original_input`** — optional verbatim phrase ("in three hours", "Friday 3pm") for
  support and for the agent to re-display when listing ("you asked for 'Friday 3pm'").
  Nullable; can be dropped if content-retention concerns outweigh debuggability.
- **`status` / `attempt_count` / `next_attempt_at` / `claimed_at`** — the delivery state
  machine (below). `next_attempt_at` is null until the first failure; the due index
  coalesces it with `due_at` so retries and first attempts share one scan.
- **`sent_at` / `telegram_message_id`** — delivery evidence. Latency
  (`sent_at - due_at`) is the primary health metric; the message ID enables future
  features (edit, reply-to) and support lookups.
- **`last_error_code`** — a *sanitized* short code (`telegram_429`, `telegram_403_blocked`,
  `network_timeout`), never a raw Telegram response body.
- **`updated_at`** — maintained by the same trigger convention the existing tables use.

### Status state machine

```text
scheduled --claim--> processing --success--> sent            (terminal)
scheduled --cancel--> cancelled                               (terminal)
processing --retryable failure--> scheduled (next_attempt_at set, backoff)
processing --permanent failure or attempts exhausted--> failed  (terminal)
processing --lease expired (claimed_at older than 5 min)--> reclaimable by next dispatcher run
```

Enforce transitions in the database — either with a trigger that rejects illegal
transitions or by doing every transition through `security definer` functions that
encode the legal edges. Illegal examples that must be impossible: `sent → scheduled`,
`cancelled → processing`, user-facing update of a row not in `scheduled`.

Cancellation and update race note: a user can cancel or edit a reminder in the same
minute the dispatcher claims it. The rule is simple and should be documented in the tool
description so the agent can phrase it honestly: **cancel/update only succeed while the
row is `scheduled`**. If the dispatcher has already moved it to `processing`, the
cancel returns "too late — it's being delivered right now," which is truthful and avoids
trying to claw back an in-flight HTTP call.

### Access control (RLS and grants)

Follow the pattern from `20260704140023_secure_backend_database_access.sql`:

- Enable RLS on `public.reminders`.
- Grant `select, insert, update` (not `delete` — cancellation is a status change, and
  keeping terminal rows preserves the audit trail; add a retention job later) to
  `jarvis_runtime`, with the standing permissive policy that role already uses.
  Ownership is enforced in the tool handlers by always filtering on the runtime
  context's `user_id`, same as the rest of the backend — RLS here is defense in depth,
  not the primary gate, because `jarvis_runtime` is a trusted backend role.
- The claim and outcome-recording functions live in the **`private` schema** and are
  granted only to the role the Edge Function connects as. `jarvis_runtime` must *not* be
  able to call them: the agent has no business claiming or completing deliveries.
- Revoke everything from `PUBLIC`, `anon`, and `authenticated`. No browser or Telegram
  client ever touches this table directly.

### Dispatcher-side database functions (in `private`)

Three functions, each a single transaction:

1. **`claim_due_reminders(batch_limit int)`** — selects rows where
   `status = 'scheduled' and coalesce(next_attempt_at, due_at) <= now()`, *or*
   `status = 'processing' and claimed_at < now() - interval '5 minutes'` (lease
   recovery), ordered by due time, `limit batch_limit`, `for update skip locked`; sets
   `status = 'processing'`, `claimed_at = now()`, increments `attempt_count`; returns
   the claimed rows (id, chat id, message, attempt_count).
2. **`record_reminder_sent(id uuid, telegram_message_id bigint)`** — transitions
   `processing → sent`, stamps `sent_at`.
3. **`record_reminder_failure(id uuid, error_code text, retryable boolean, retry_after_seconds int default null)`** —
   if retryable and `attempt_count < max_attempts`: transition back to `scheduled` and
   set `next_attempt_at` (honoring Telegram's `retry_after` when provided, otherwise the
   backoff schedule); else transition to `failed`.

Putting attempt-count increment inside the *claim* (not the outcome) is deliberate: if
the function crashes after claiming, the lease-recovery path already counted that
attempt, so a crash-looping dispatcher still exhausts the retry budget instead of
retrying forever.

## Agent Tools

Register a `reminders` domain (first-party, per the note above) exposing four tools:

- `create_reminder(message, due_at_local, timezone?)`
- `list_reminders(status?, from?, to?)`
- `update_reminder(reminder_id, message?, due_at_local?)`
- `cancel_reminder(reminder_id)`

### Argument and trust rules

- The runtime context (`RuntimeContextSnapshot`) supplies the canonical `user_id`, the
  configured `timezone`, and the Telegram chat ID. **These are injected by the handler
  and must never be accepted from model-generated tool arguments** — same rule the
  Todoist and Calendar handlers already follow.
- `timezone` as a tool argument exists only for the rare explicit override ("remind me
  at 9am New York time"); default is the snapshot timezone. The handler validates it
  against the IANA database and stores whichever was actually used.
- The model passes `due_at_local` as an explicit local date-time it has already resolved
  ("2026-07-08T15:00"). The prompt fragment for this domain should instruct the model to
  resolve relative phrases itself using the current date-time and timezone that the
  system prompt already carries, and to ask a clarifying question when the phrase is
  genuinely ambiguous ("remind me Friday" — morning? a specific time?). The *handler*
  then converts local → UTC. Splitting it this way keeps fuzzy language interpretation
  in the LLM where it belongs and deterministic timezone math in Python where it is
  testable.

### Handler validation (deterministic, tested)

- Reject `due_at` in the past, with a small grace window (about 30 seconds) so "remind
  me now-ish" phrasing doesn't fail on clock skew.
- Reject `due_at` beyond a far horizon (e.g. one year) — almost always a
  misinterpretation, and the clarification flow is the right response.
- Enforce the 1–4096 character message bound before insert so the user gets a
  conversational error rather than a database constraint failure.
- DST correctness: local → UTC conversion must use proper IANA rules, and the two edge
  cases need explicit tests — a nonexistent local time (spring-forward gap: shift
  forward to the first valid instant) and an ambiguous local time (fall-back overlap:
  pick the first occurrence). Document the chosen policy in the handler.
- `update_reminder` re-runs the same validation on any changed field, and both update
  and cancel must match on `id` *and* the context `user_id`, returning not-found (never
  "belongs to someone else") on a miss.

### Reminder identity in conversation

Users will say "cancel that reminder" or "move the second one to 5pm", not quote UUIDs.
The `list_reminders` result should therefore return stable IDs alongside display text,
and the domain should plug into the existing entity-reference machinery
(`graph/entity_index.py` / `canonicalize.py`) the same way task references do, so a
follow-up like "cancel it" resolves to the right row. `list_reminders` output should
render times in the reminder's own stored timezone.

### Risk classification and confirmation

Slot into the existing deterministic policy in `graph/risk.py` and `tools/metadata.py`:

- `create_reminder` and `update_reminder` are **mutating but not always-risky**: a
  single call executes without the confirm gate (consistent with creating a Todoist
  task), and crossing the bulk threshold in one turn routes to the confirm gate like any
  other domain.
- `cancel_reminder` is reversible in practice (the user can re-create) — classify as a
  normal mutation, not `always_risky`. If product feedback shows accidental
  cancellations, flipping one metadata flag upgrades it.
- Even without a HITL gate, the assistant's *reply* after creation must restate the
  interpretation so misparses surface immediately:

  > I'll remind you to submit the form on Tue 8 July at 3:00 PM (Asia/Singapore).

  This restated confirmation is the primary defense against silent time-parsing errors
  and costs nothing.

### Prompt fragment

The domain's `prompt_fragment` should cover: reminders are one-time only (politely
decline recurring requests for now and say why); always restate the resolved absolute
time in the user's timezone; ask rather than guess when a time expression is ambiguous;
"remind me about X" means the message should be a self-contained imperative ("Submit the
certification form"), not a transcript fragment.

## Dispatch Flow

1. `pg_cron` fires every minute and uses `pg_net` to POST to the `reminder-dispatch`
   Edge Function URL. The request carries a scheduler-only shared secret in a header;
   the secret is stored in Supabase Vault and referenced by the cron job's SQL, so it
   never appears in the cron job definition in plaintext.
2. The Edge Function rejects any request without the exact secret (constant-time
   comparison). This function is *not* meant to be callable by anything else.
3. It calls `private.claim_due_reminders(batch_limit := 25)`. If zero rows return, it
   logs a heartbeat-level event and exits — the common case.
4. For each claimed row, it calls Telegram `sendMessage` with the chat ID and message
   text. Sends within a batch run sequentially, not concurrently — Telegram's per-bot
   rate limits make a sequential loop the simplest safe policy at this scale, and a
   25-row batch completes in a few seconds.
5. On success, it calls `record_reminder_sent` with the returned message ID.
6. On failure, it maps the response to an error code and retryability (table below) and
   calls `record_reminder_failure`.
7. It logs one structured summary line: claimed count, sent count, failed count,
   duration.

### Batch size, lease, and backoff parameters

| Parameter | Initial value | Rationale |
|---|---|---|
| Cron cadence | 1 minute | Matches the delivery-latency promise. |
| Batch limit | 25 | Far above expected volume; bounds worst-case runtime well under the cron period so runs don't stack. |
| Lease timeout | 5 minutes | An Edge Function invocation is capped well below this; anything still `processing` after 5 minutes is dead and safe to reclaim. |
| Max attempts | 5 | Enough to ride out a Telegram incident of tens of minutes without spamming retries for days. |
| Backoff schedule | 1, 2, 4, 8 minutes (then failed) | Exponential from the cron granularity floor; a sub-minute backoff is meaningless when the dispatcher only wakes every minute. |
| `retry_after` override | Honor Telegram's value when present on 429 | Telegram tells you exactly when to come back; guessing is worse. |

If a run ever claims a full batch, it should log that fact distinctly (possible backlog)
— but must **not** loop for another batch within the same invocation in v1. Draining
loops interact badly with function time limits; the next minute's run picks up the rest.

### Telegram error mapping

| Telegram response | Classification | Action |
|---|---|---|
| 200 OK | success | `sent`, store message ID |
| 429 Too Many Requests | retryable | back to `scheduled`, `next_attempt_at = now() + retry_after` |
| 5xx | retryable | back to `scheduled`, backoff schedule |
| Network error / timeout | retryable | same as 5xx; note this is the one case where the message may have been delivered without us knowing (see idempotency) |
| 403 (bot blocked by user) | permanent | `failed`, code `telegram_403_blocked` |
| 400 chat not found | permanent | `failed`, code `telegram_400_chat_not_found` |
| 400 other (malformed) | permanent | `failed`, code `telegram_400_bad_request` — should be impossible given the DB length check; if it happens, it's a bug to investigate |

## Delivery Format

The first version sends static text:

```text
⏰ Reminder

Submit the certification form.
```

Send as plain text — no Markdown/HTML `parse_mode`. The message body is user-authored
free text, and Telegram rejects messages with invalid entity markup; plain text
eliminates an entire class of permanent 400 failures. Rich formatting can come later
with proper escaping.

Direct Telegram delivery is faster, cheaper, and more reliable than invoking the full
agent. A future reminder type may request fresh context ("At 5 PM, summarize tomorrow's
calendar"); that should enqueue a durable *agent job*, a separate mechanism, rather than
extending this dispatcher.

One consequence worth stating: a reminder delivered this way is **not** part of any
LangGraph thread. If the user replies to the reminder message, the Node webhook treats
it as a normal new message. That is acceptable for v1; the future snooze/dismiss buttons
would use `callback_data` carrying the reminder ID to bridge back.

## Failure and Idempotency Rules

- Atomic claims (`FOR UPDATE SKIP LOCKED` in one transaction) prevent two dispatcher
  instances from processing the same row concurrently.
- Lease recovery reclaims rows stuck in `processing` past 5 minutes, so a dispatcher
  that dies between claim and outcome cannot strand work.
- Attempt count increments at claim time, so crash loops consume the retry budget.
- Cap attempts at 5; exhaustion is `failed`, never silent drop.
- Updating or cancelling requires both the reminder ID and the current user's canonical
  `user_id`, and only succeeds from `scheduled`.
- Never log reminder text, bot tokens, chat IDs beyond what debugging requires, or full
  Telegram response bodies in infrastructure logs.

**Delivery semantics: at-least-once with strong duplicate reduction, not exactly-once.**
Exactly-once is impossible across an external HTTP call and a database write. The one
real duplicate window: Telegram accepts the message, and the function dies (or the
network drops the response) before `record_reminder_sent` commits. The row is later
reclaimed and re-sent. This window is a few hundred milliseconds per send, so duplicates
will be rare, and a duplicate reminder is a benign failure for this product. Do not
attempt to close it in v1 (Telegram offers no idempotency key on `sendMessage`); just
document it and count it if it ever shows up in support.

The reverse failure — marked `failed` but actually delivered — cannot happen: failure
recording only follows an explicit Telegram error response.

## Security

- Store `TELEGRAM_BOT_TOKEN` as a Supabase Edge Function secret; it must not appear in
  migrations, cron definitions, or logs. This is a second holder of the bot token
  (alongside the Node service's env) — note it in the secret-rotation runbook so
  rotation updates both.
- Store the scheduler shared secret in Supabase Vault; the cron SQL reads it from Vault
  at fire time.
- The Edge Function authenticates every request against that secret before touching the
  database, and deploys with JWT verification appropriate to a non-user-facing endpoint.
- Claim/outcome functions live in `private`, are `security definer` with a pinned
  `search_path`, and have `execute` revoked from `PUBLIC`, `anon`, `authenticated`, and
  `jarvis_runtime`.
- RLS is enabled on `reminders` even though only privileged roles touch it.
- Message length and status transitions are validated in the database, not only in
  application code.
- After the migration lands, run the Supabase security advisors (`get_advisors`) — the
  project already treats that as a standard post-DDL check.

## Observability

Structured events (Edge Function and tool handlers, following the existing
`domain.action.outcome` log naming):

- `reminder.created` / `reminder.updated` / `reminder.cancelled` (tool handlers; log the
  reminder ID and due time, not the message text)
- `reminder.dispatch.run` (per invocation: claimed, sent, failed, reclaimed-lease
  counts, duration)
- `reminder.dispatch.sent` (id, latency `sent_at - due_at`)
- `reminder.dispatch.retry` (id, attempt, error code, next attempt)
- `reminder.dispatch.failed` (id, final error code)

Metrics to watch after launch:

- Dispatch latency distribution (`sent_at - due_at`) — the product promise is ~1 minute;
  p95 above ~90 seconds means the cron or claim path is unhealthy.
- Failure rate by error code — a spike in `telegram_403_blocked` is user behavior; a
  spike in retryables is infrastructure.
- Stale-lease recoveries — should be ~0; any steady rate means the function is dying
  mid-run.
- Rows in `scheduled` with `coalesce(next_attempt_at, due_at)` more than 5 minutes in
  the past — this is the single best health check ("is anything due and unclaimed?")
  and the condition to alert on, because it catches a dead cron job, a broken secret,
  and a crashing function all at once.

## Testing Strategy

Follow the repo's existing split (unit vs `test:integration` vs env-flag-gated live):

**Python unit tests** (tool handlers): timezone conversion including both DST edge
cases, past/far-future rejection, message length bounds, ownership filtering on
update/cancel, injected-vs-model-supplied argument separation, risk classification of
the four tools.

**Database tests** (against a local Supabase stack): claim atomicity under two
concurrent claimers (each row claimed exactly once), lease recovery, illegal status
transitions rejected, backoff arithmetic in `record_reminder_failure`, RLS/grant matrix
(`jarvis_runtime` can CRUD but cannot execute `private.claim_due_reminders`).

**Edge Function tests** (local `supabase functions serve` with a mocked Telegram
endpoint): auth rejection without the secret, success path records message ID, 429 with
`retry_after` honored, 403 marks failed, network timeout returns row to scheduled,
full-batch logging.

**End-to-end (live-gated, single user)**: create a reminder due in 2 minutes through
the real Telegram conversation, observe delivery and latency; cancel one before
delivery; race an update against the due minute.

## Implementation Sequence

Each step lands independently and is verifiable before the next:

1. **Migration**: `reminder_status` enum, `reminders` table, indexes, RLS, grants to
   `jarvis_runtime`, `updated_at` trigger, status-transition enforcement, and the three
   `private` functions. Verify with the database tests and `get_advisors`.
2. **Python domain**: repository methods, the four tool specs/handlers, metadata entries,
   prompt fragment, first-party adapter registration (including the
   `requires_credential` contract extension), entity-index hookup. Unit tests.
3. **Conversation pass**: exercise create/list/update/cancel through the real agent
   locally (no dispatcher yet — rows just sit in `scheduled`). This validates parsing
   and confirmation phrasing before any delivery infrastructure exists.
4. **Edge Function**: implement `reminder-dispatch` under `supabase/functions/` (new
   directory — this is the project's first Edge Function, so it also establishes the
   local dev + deploy workflow). Test locally against the local stack with mocked
   Telegram.
5. **Scheduling**: Vault secret, Edge Function secrets, `pg_cron` + `pg_net` job.
   Confirm the minutely heartbeat log appears.
6. **Failure-mode verification**: overlapping dispatchers, kill-mid-run lease recovery,
   429/403 mapping, retry exhaustion, cross-user access attempts.
7. **Rollout**: enable for Jerry only, watch latency and error metrics for a few days,
   then enable for Zachary. (Rollout gating can be as simple as the tool domain being
   activated per user.)

## Open Questions

- **Retention**: terminal rows (`sent`/`cancelled`/`failed`) accumulate forever. Decide
  a retention window (e.g. 90 days) and add a cleanup cron later; not a launch blocker
  at this volume.
- **`original_input` retention**: keep for debuggability, or drop to minimize stored
  user content? Default: keep, revisit with retention policy.
- **Quiet hours**: deliberately out of scope, but the preferences schema
  (`AssistantPreferencesV1`) is where it would live — worth keeping in mind so the
  dispatcher's claim function can grow a "not within quiet hours" predicate without
  schema surgery.

## Future Extensions

- Recurring reminders: an explicit recurrence rule column plus a computed `next_due_at`;
  on send, the dispatcher writes the next occurrence instead of terminal `sent`.
- Snooze and dismiss inline buttons via Telegram callback data carrying the reminder ID.
- Quiet hours and per-user delivery preferences.
- Delivery channels other than Telegram.
- Agent-generated reminders that enqueue a durable background agent run at fire time.
- A unified scheduled-jobs abstraction shared with daily briefs — extract it only once
  both concrete systems exist and the shared shape is proven, not before.
