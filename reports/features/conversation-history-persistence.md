# Plan: Durable, Queryable Conversation History in Supabase

## Status

Proposed implementation plan for the current Jarvis architecture and its initial
scale target of up to 12 concurrent users/runs.

## Decision summary

Add a relational `conversation_messages` read model for the user-visible
conversation while keeping LangGraph checkpoints as the authoritative execution
state.

The first version will persist immutable application-level conversation events:

- The raw user message submitted to an invocation
- A clarification or confirmation reply submitted to a resume
- The final assistant response
- A clarification or confirmation prompt when a run interrupts

It will not copy `result["messages"]` wholesale. That array is model execution
state and includes system prompts, timestamp/context wrappers, synthetic HITL
messages, and tool payloads. Those are unsuitable for a user-visible transcript
and may contain sensitive or mutable internal data.

Persistence will use the existing async Postgres pool and will be awaited after
the graph finishes but before `run_jarvis_async` returns. At the current maximum
of 12 concurrent runs, one short batched insert is preferable to adding a
durable job system. The existing best-effort `post_run.py` queue remains
appropriate for metadata and usage telemetry, but not as the only persistence
path for conversation history because it may drop jobs under saturation or lose
queued work during an abrupt process exit.

## Goals

- Query a thread's user-visible conversation without decoding checkpoint blobs
- Query conversation activity through the existing `threads -> users`
  ownership relationship
- Preserve correct ordering across invoke and resume requests
- Make retries and duplicate request delivery idempotent
- Avoid storing system prompts or synthetic orchestration content
- Preserve the existing request safety model if history persistence fails after
  a graph run may have executed mutations
- Reuse existing database pools, RLS provisioning, shutdown behavior, and
  logging/tracing facilities

## Non-goals

- Replacing LangGraph checkpoints
- Reconstructing graph execution from `conversation_messages`
- Persisting chain-of-thought or `reasoning_content`
- Treating raw tool output as long-term memory
- Providing semantic search, embeddings, or memory retrieval in this change
- Backfilling every historical checkpoint in the initial migration
- Exposing conversation rows directly through the Supabase Data API

Tool execution history can be added later as a separate internal event model
with explicit redaction, retention, and access rules. It should not be mixed
into the public conversation transcript by default.

## Why not persist `result["messages"]`

The LangGraph message array is optimized for model execution rather than
application history:

- Index `0` is a system prompt and may be rebuilt by router prompt slimming.
- The initial user message is decorated with request time and reply context.
- HITL resumes append synthetic tool messages and a synthetic clarification
  summary in addition to the actual reply.
- Tool results may contain full email, calendar, task, or other private payloads.
- Message positions describe the current state array, not a durable event
  identity.

Consequently, `(thread_id, turn_index)` is not a sufficient idempotency key and
`ON CONFLICT DO NOTHING` could silently preserve the wrong representation.

## Data model

Create a new migration in `supabase/migrations/`.

```sql
create table public.conversation_messages (
  id            bigint generated always as identity primary key,
  thread_id     text not null
                  references public.threads(thread_id)
                  on delete cascade,
  request_id    text not null,
  event_index   smallint not null,
  role          text not null,
  message_kind  text not null,
  content       text not null,
  occurred_at   timestamptz not null,
  persisted_at  timestamptz not null default now(),
  metadata      jsonb not null default '{}'::jsonb,

  constraint conversation_messages_request_event_unique
    unique (thread_id, request_id, event_index),
  constraint conversation_messages_event_index_check
    check (event_index >= 0),
  constraint conversation_messages_role_check
    check (role in ('user', 'assistant')),
  constraint conversation_messages_kind_check
    check (
      message_kind in (
        'user_message',
        'assistant_message',
        'clarification_reply',
        'clarification_prompt',
        'confirmation_reply',
        'confirmation_prompt'
      )
    ),
  constraint conversation_messages_content_check
    check (
      btrim(content) <> ''
      and octet_length(content) <= 262144
    ),
  constraint conversation_messages_metadata_object_check
    check (jsonb_typeof(metadata) = 'object')
);

create index conversation_messages_thread_timeline_idx
  on public.conversation_messages (thread_id, occurred_at, id);
```

### Ownership and deletion

`threads` remains the canonical owner of a conversation. `user_id` is
intentionally not duplicated on `conversation_messages`; user-level queries join
through `threads`, which already has an index on `(user_id, last_activity_at)`.
This prevents a message from naming one user while referencing another user's
thread.

`ON DELETE CASCADE` makes existing user and expired-thread cleanup remove
conversation messages automatically:

`users -> threads -> conversation_messages`

### Ordering and idempotency

`request_id` is already generated for every run. A single invocation emits at
most two public events:

| `event_index` | Event |
|---:|---|
| `0` | User message or resume reply |
| `1` | Assistant response or interrupt prompt |

The unique constraint on `(thread_id, request_id, event_index)` makes a replay of
the same accepted request a no-op without relying on mutable LangGraph array
positions.

Timeline queries order by `occurred_at, id`. The user event receives the run's
`started_at`; the assistant event receives `finished_at`.

### Metadata

Keep metadata small and non-sensitive. Initial fields may include:

- `request_source`
- `invocation_type` (`invoke` or `resume`)
- `interrupt_type` (`clarify` or `confirm`) when applicable
- `run_status` (`completed`, `interrupted`, or `failed`)

Do not store credentials, complete runtime snapshots, system prompts, reasoning
content, raw tool results, or Telegram profile identifiers in this column.

## Application design

### 1. New module: `agents/agent_api/app/conversation_history.py`

Implement two focused functions:

```python
def build_conversation_events(
    *,
    thread_id: str,
    request_id: str,
    request_source: str,
    resuming: bool,
    resume_interrupt_type: str | None,
    user_prompt: str,
    clarification_reply: str | None,
    result: JarvisState,
    started_at: datetime,
    finished_at: datetime,
) -> list[ConversationEvent]:
    ...


async def persist_conversation_events(
    events: Sequence[ConversationEvent],
) -> None:
    ...
```

`build_conversation_events` is pure and owns the transcript semantics:

- For an invoke, event `0` is the unmodified `user_prompt`.
- For a resume, event `0` is the unmodified `clarification_reply`. Its kind is
  selected from the captured pre-resume interrupt type: `clarify` maps to
  `clarification_reply` and `confirm` maps to `confirmation_reply`.
- For a completed run, event `1` is `result["final_response"]`.
- For a clarification interrupt, event `1` is the interrupt's `question`.
- For a confirmation interrupt, event `1` is the interrupt's `summary`.
- Empty assistant responses are not inserted.
- `result["messages"]` and `reasoning_content` are never inspected.

`persist_conversation_events`:

- Uses `get_async_pool()`
- Inserts all events for the run in one transaction and one batched statement
- Uses `ON CONFLICT (thread_id, request_id, event_index) DO NOTHING`
- Lets database errors propagate to the caller so failure is observable
- Does not create a new pool, executor, queue, or logging sink

The insert should use parameterized SQL. No message content or metadata should
be interpolated into SQL strings or written to diagnostic logs.

### 2. Hook into `run_jarvis_async`

For a resume, capture the pending interrupt type from LangGraph's pre-resume
state snapshot/interrupt metadata before invoking `Command(resume=...)`. Accept
only `clarify` or `confirm` and fail closed if the checkpoint does not contain a
valid pending interrupt. Do not infer the type from reply text or from outcome
fields that may survive an earlier interrupt.

After `result = enrich_interrupt_status(...)` and after `finished_at` is
captured:

1. Build the public events from the invocation inputs and final result.
2. Await the single batched insert.
3. Emit a success or failure event through the existing run tracer.
4. Continue using the existing post-run queue for thread metadata and usage.

Use the canonical thread ownership already persisted by
`store_thread_context_async`; do not read a nonexistent `identity.user_id` and
do not trust the request's free-form `user_id` as a database UUID.

### 3. Failure policy

History persistence happens after the graph has completed. The graph may have
already performed an external mutation, so a history write failure must not
turn a successful run into a replayable failed request.

Apply this policy:

- Attempt the awaited batched write.
- On failure, emit a redacted tracer event containing `request_id`,
  `thread_id`, and the exception type only.
- Mark an internal result field such as `history_persisted = false` for
  diagnostics, but preserve the graph's terminal response.
- Do not log message content.
- Keep checkpoints as the recovery source.

This provides immediate persistence during normal operation without risking a
duplicate external mutation. If conversation history later becomes a contractual
source of record, add a durable outbox/reconciliation worker rather than moving
the write back to the lossy post-run queue.

### 4. Runtime readiness

Add `conversation_messages` to `_REQUIRED_RUNTIME_TABLES` in
`agents/agent_api/app/db.py`. Startup should fail clearly when application code
is deployed before the migration or when the runtime role cannot access the
table.

The existing `private.rls_auto_enable` event trigger already:

- Enables RLS for new tables in `public`
- Grants CRUD to `jarvis_runtime`
- Creates the standard runtime policy
- Grants identity-sequence usage

The migration should still be verified under `jarvis_app` during integration
testing. Current RLS is backend-role isolation, not per-end-user Data API
isolation. Any future direct client access requires separate user-scoped
policies.

## Files to modify

| File | Change |
|---|---|
| `supabase/migrations/<timestamp>_add_conversation_messages.sql` | Create the table, constraints, foreign key, and timeline index |
| `agents/agent_api/app/conversation_history.py` | Define, build, and batch-persist public conversation events |
| `agents/agent_api/app/graph/builder.py` | Await history persistence at run completion and trace the outcome |
| `agents/agent_api/app/graph/state.py` | Add the optional internal `history_persisted` diagnostic field if retained in the result |
| `agents/agent_api/app/db.py` | Add the table to runtime readiness checks |
| `tests/agents/test_conversation_history.py` | Unit tests for event construction, idempotency SQL, and failure behavior |
| Relevant database integration tests | Verify migration, privileges, constraints, ownership, and cascades |

No TypeScript change is required for the initial persistence path because the
Python service already receives the raw user input, request ID, thread ID,
request source, and resume reply.

## Verification

### Unit tests

1. Fresh completed invoke produces exactly:
   - Raw user message
   - Final assistant response
2. Clarification interrupt produces:
   - Raw user message
   - Clarification prompt
3. Clarification resume produces:
   - Raw clarification reply
   - Final response or next clarification prompt
4. Confirmation interrupt and approve/decline resumes use confirmation kinds.
5. System prompts, wrapped user prompts, tool results, and reasoning content
   never appear in persisted events.
6. Empty assistant content does not create an empty row.
7. Repeating the same `(thread_id, request_id, event_index)` is idempotent.
8. A persistence exception is traced and does not replace a successful graph
   result.

### Database integration tests

1. Apply the migration and run database readiness as `jarvis_app`.
2. Verify `jarvis_runtime` can select and insert rows.
3. Verify `anon` and `authenticated` cannot access the table.
4. Verify invalid roles, kinds, indexes, metadata shapes, empty content, and
   oversized content are rejected.
5. Verify a message cannot reference a nonexistent thread.
6. Verify deleting a thread deletes its messages.
7. Verify deleting a user cascades through threads to messages.
8. Verify user-level history queries join through `threads` and cannot return
   another user's rows when the backend ownership filter is applied.

### End-to-end checks

1. Send a normal Telegram request and query the stored transcript.
2. Complete an invoke -> clarification -> resume flow and verify ordering.
3. Complete an invoke -> confirmation -> approve and decline flow.
4. Replay an existing API `request_id` and verify no duplicate rows.
5. Restart the service normally and verify runtime readiness and pool shutdown.
6. Run the existing Python and TypeScript suites.
7. Run 12 concurrent invocations and verify:
   - No missing transcript rows during normal database operation
   - No duplicate events
   - No database pool starvation
   - Acceptable added completion latency
   - No sensitive content in logs

Useful manual query:

```sql
select
  message.thread_id,
  message.request_id,
  message.event_index,
  message.role,
  message.message_kind,
  left(message.content, 120) as content_preview,
  message.occurred_at
from public.conversation_messages message
where message.thread_id = '<thread-id>'
order by message.occurred_at, message.id;
```

User-level query:

```sql
select message.*
from public.conversation_messages message
join public.threads thread
  on thread.thread_id = message.thread_id
where thread.user_id = '<canonical-user-uuid>'
order by message.occurred_at desc, message.id desc;
```

## Rollout

1. Apply the migration.
2. Verify runtime readiness and table privileges using the production-style
   `jarvis_app` connection.
3. Deploy the application code.
4. Exercise one invoke and one resume flow.
5. Monitor persistence failures and completion latency.
6. Run the 12-concurrent-invocation check before enabling any consumer that
   treats the table as complete.

Historical checkpoint backfill, tool-event storage, retention reporting,
semantic search, and long-term memory ingestion should be separate follow-up
changes with their own privacy and correctness reviews.


