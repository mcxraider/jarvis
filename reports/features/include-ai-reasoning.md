# Revised Plan: Capture and Propagate AI Reasoning

This replaces the earlier opt-in plan. There will be no `include_reasoning` parameter and no reasoning-specific behavior yet.

The system will always capture DeepSeek’s raw `reasoning_content` and carry it beside the normal model response through every existing response-delivery layer. Telegram will continue rendering only the normal answer. This leaves future consumers free to display, persist, analyze, or discard reasoning without changing the model/API pipeline again.

## 1. Response contract

Treat the model output as a paired payload:

```text
response              Normal user-facing answer
reasoning_content     Raw DeepSeek reasoning associated with that answer
```

Public and internal names:

- Python/API: `reasoning_content: Optional[str]`
- TypeScript: `reasoningContent?: string`
- No request parameter will be added.
- No feature flag or environment variable will be added.
- When DeepSeek supplies non-empty reasoning, it travels with the response automatically.
- When no reasoning exists, the field remains absent or `null` according to the existing serializer.
- The normal `response` field remains unchanged.

This pairing applies to:

- Completed answers
- Clarification interrupts
- Confirmation interrupts
- Responses produced after clarification or confirmation resumes
- Standard HTTP responses
- NDJSON streaming final events
- Bulk invoke results
- Cached idempotent responses
- Text and audio Telegram processing paths
- Inline-button confirmation callback resumes

## 2. Run-scoped reasoning capture

Introduce a small run-scoped collector owned by `run_jarvis`.

The collector will be created fresh for every invoke or resume and passed through graph construction into the orchestrator node. It must not be stored in LangGraph checkpoint state.

For each model call:

1. Clear the collector before requesting a new completion.
2. Receive the complete raw assistant message from DeepSeek.
3. Read `reasoning_content` without transforming, summarizing, or logging it.
4. Normalize only its presence:
   - Preserve a non-empty string exactly.
   - Treat missing, `null`, or empty values as unavailable.
5. Replace the collector’s previous value with the current model turn’s value.

Overwriting on every model call ensures that a multi-turn tool workflow returns reasoning from the latest model response rather than an earlier planning/tool-call turn.

Clearing before each call also prevents earlier reasoning from being attached when the final model call fails.

After graph execution:

- Add the captured value to the returned runtime result as `reasoning_content`.
- Do this after interrupt enrichment so completed and interrupted runs use the same output path.
- Only attach reasoning produced during the current invocation.
- Never recover reasoning by scanning checkpointed message history.

This prevents stale reasoning in cases such as:

- A resumed confirmation is declined without another model call.
- A resumed graph returns a deterministic message.
- An earlier model turn succeeded but the final model request failed.
- A thread contains reasoning from previous clarification cycles.

Expected behavior:

| Run outcome | Reasoning result |
|---|---|
| Direct model answer | Reasoning from that answer |
| Tools followed by final model answer | Reasoning from the final model call |
| Clarification interrupt | Reasoning from the model call that requested clarification |
| Confirmation interrupt | Reasoning from the model call that proposed the risky action |
| Resume followed by a new answer | Reasoning from the new model call |
| Confirmation declined without a model call | No reasoning |
| Deterministic validation/gate response | No reasoning |
| Model failure | No stale reasoning |

## 3. Python API propagation

Extend `JarvisState` with an output-only optional `reasoning_content` field for typing. It will not be initialized in graph state or checkpointed.

Extend `AgentResponse` with:

```python
reasoning_content: Optional[str] = None
```

Update the shared result-to-response conversion so reasoning is copied alongside `response` for completed and interrupted results.

All routes already converge on the shared conversion and serialization helpers, so reasoning must flow through:

- `POST /invoke`
- `POST /invoke/stream`
- `POST /resume`
- `POST /resume/stream`
- `POST /invoke-bulk`

Streaming behavior:

```json
{
  "type": "final",
  "response": {
    "status": "completed",
    "thread_id": "...",
    "response": "Done.",
    "reasoning_content": "..."
  }
}
```

Progress events will not contain reasoning.

Failure and fallback responses will leave reasoning unset unless the outward response genuinely came from a successful current-run model turn.

## 4. Idempotency behavior

Reasoning will be part of the existing cached `AgentResponse`, exactly like the normal response text.

When an invoke or resume is replayed with the same idempotency identity:

- The graph must not execute again.
- The cached response text must be returned.
- The matching cached reasoning must be returned with it.
- No separate reasoning cache or key is needed.
- The idempotency-key algorithm remains unchanged.

Because there is no retrieval parameter, there is no projection or conditional-cache problem from the earlier plan.

## 5. TypeScript API client propagation

Extend `AgentResponseSchema` with:

```typescript
reasoning_content: z.string().nullish()
```

Extend `LangGraphAgentResponse` with:

```typescript
reasoningContent?: string;
```

Update the shared normalization function to map:

```text
reasoning_content → reasoningContent
```

This automatically covers:

- Normal invoke responses
- Normal resume responses
- Streamed invoke final events
- Streamed resume final events
- Idempotency replays received by the client

Fallback responses will omit `reasoningContent`.

No changes will be made to `LangGraphAgentRequest` or serialized request payloads.

## 6. Telegram processing propagation

Extend `TextProcessorResult` with:

```typescript
reasoningContent?: string;
```

Both fresh invokes and pending-thread resumes will copy `agentResponse.reasoningContent` into their result alongside `response`.

The field must survive:

- Buffered-message suffix handling
- Clarification interrupts
- Confirmation interrupts
- Fresh text requests
- Clarification replies
- Audio transcription followed by agent processing
- Audio-document transcription followed by agent processing
- `MessageProcessorService` delegation

The audio processor currently reconstructs `TextProcessorResult` field by field, so both audio return paths must explicitly copy `reasoningContent`. Otherwise audio requests would silently drop it.

For confirmation-button callbacks, `CallbackHandler` already retains the full `LangGraphAgentResponse`; reasoning will remain available on that response object while normal text continues through the existing send functions.

## 7. Telegram rendering boundary

Reasoning must stop at the final Telegram rendering boundary for now.

The following functions continue receiving only the normal text:

- `sendFinalReply`
- `sendClarificationReply`
- Confirmation reply rendering

No reasoning will be:

- Appended to Telegram text
- Added to captions or Markdown
- Sent as a second message
- Added to inline-button callback data
- Included in progress indicators
- Included in error messages
- Added to response-length logging

Handlers will still possess `reasoningContent` immediately before this boundary, creating a future integration point for rendering or storage without another backend/API change.

## 8. Persistence boundaries

Reasoning will be included in the existing request-idempotency response cache because that cache stores the complete API response.

No new durable product storage will be introduced:

- No database migration
- No reasoning history table
- No analytics sink
- No Telegram pending-clarification column
- No new file logging
- No LangGraph checkpoint field

The pending-clarification store remains focused on information required to resume and collapse Telegram prompts. It is not an archival response store.

If persistent reasoning becomes a future use case, the handler or API boundary can store the already-propagated `reasoningContent`.

## 9. Logging and privacy

Raw reasoning must never be added to logs.

Existing presence-only diagnostics such as `has_reasoning` can remain. New tests may validate propagation, but production diagnostics should record at most:

- Whether reasoning was present
- Optionally its character length, only if needed later

Do not log the content itself through:

- Python run logging
- TypeScript async logger
- Progress metadata
- Error details
- Ad-hoc debug output

No new logging path is required for this feature.

## 10. Test plan

### Graph capture

Add tests covering:

- Direct final answer captures reasoning.
- Tool-call reasoning is replaced by final-answer reasoning.
- Clarification interrupt returns its current reasoning.
- Confirmation interrupt returns reasoning from the risky-action model turn.
- Resume with a new model call returns only the new reasoning.
- Resume decline without a model call returns no old reasoning.
- Final model failure does not return reasoning from a prior successful turn.
- Missing or empty provider reasoning remains unset.

### Python API

Cover completed and interrupted responses for:

- `/invoke`
- `/invoke/stream`
- `/resume`
- `/resume/stream`
- `/invoke-bulk`

Verify:

- Raw reasoning is serialized without modification.
- Responses without reasoning remain valid.
- Progress events never contain reasoning.
- Failed fallback responses do not fabricate reasoning.

### Idempotency

Add a replay test that:

1. Executes a request returning response text and reasoning.
2. Repeats the same request ID.
3. Confirms `run_jarvis` ran once.
4. Confirms both cached fields are identical on replay.

### TypeScript contract and client

Update Python Pydantic and TypeScript Zod contract fixtures/tests to cover:

- Response with reasoning
- Response without reasoning
- Interrupted response with reasoning

Update client tests to verify:

- Non-streamed normalization
- Streamed-final-event normalization
- Missing reasoning remains `undefined`
- Request payloads remain unchanged

### Telegram propagation

Test that:

- Text invoke results expose `reasoningContent`.
- Text resume results expose `reasoningContent`.
- Voice and audio-document paths preserve it through their reconstructed result objects.
- Buffered-message modifications change only `response`.
- Message handlers still send only `response`.
- Clarification and confirmation Telegram messages never contain reasoning.
- Callback resumes retain reasoning on the client result but do not render it.

## 11. Validation commands

Run:

```bash
pytest \
  tests/agents/test_jarvis.py \
  tests/agents/test_api.py \
  tests/agents/test_contract.py \
  tests/agents/test_request_idempotency.py
```

Run the focused TypeScript suites for:

- Agent contract
- LangGraph client
- Text processor
- Audio processor
- Message handlers
- Callback handler

Then run:

```bash
npm run build
git diff --check
```

Existing unrelated worktree changes must remain untouched. Implementation should edit and stage only the files belonging to this feature.


## Possible feature
- include in clarification, why need calrification. 
Clarification needed:
--------------------
Did you mean these Todoist dinner tasks (not Google Calendar events)? Should I create reminder tasks 2 hours before each of them?
Reason: Google Calendar is empty this week, but you have two dinner-related Todoist tasks. Need to clarify which source you meant.
Your reply: 