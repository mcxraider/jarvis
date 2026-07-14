



The leak appears genuine in the limited sense that **Claude Code v2.1.88’s TypeScript source was accidentally exposed through a production source map on March 31, 2026**. The archive contained roughly 1,884–1,900 files and about 512,000 lines. However, the mirrors are unofficial, some internal packages were absent, and several community analyses make inferences that Anthropic has not independently confirmed. Treat exact filenames and unreleased feature interpretations as provisional. citeturn220825view0turn220825view4turn873598news89

The useful takeaway is not “Claude Code uses a ReAct loop.” It is the collection of low-level production decisions surrounding that loop.

# The most important low-level findings

## 1. The main runtime is one async event stream

Claude Code does not appear to maintain separate implementations for the CLI, SDK, IDE bridge and headless mode. All interfaces converge on one `queryLoop`, implemented as an async generator.

Conceptually:

```ts
async function* queryLoop(state) {
  while (true) {
    state = await shapeContext(state);

    const stream = callModel(state);

    for await (const event of stream) {
      yield event;

      if (event.type === "tool_use") {
        const result = await dispatchTool(event);
        state.messages.push(asToolResult(result));
      }
    }

    if (shouldStop(state)) {
      return;
    }
  }
}
```

The generator emits a unified stream containing things such as:

```ts
type AgentEvent =
  | TextDelta
  | ThinkingDelta
  | ToolCallStarted
  | ToolCallProgress
  | ToolCallCompleted
  | PermissionRequested
  | ContextCompacted
  | RecoverableError
  | FinalResponse;
```

This lets the CLI, IDE, API and other front ends consume the same execution semantics and merely render them differently. Subflows can be composed using generator delegation rather than introducing another orchestration system. citeturn578300view6turn385820view0

### Takeaway for your Jarvis

Your LangGraph state machine is currently doing two jobs:

1. controlling execution;
2. representing frontend-visible progress.

Separate them.

Keep the graph as the durable execution state, but make every node emit a normalized event:

```python
class AgentEvent(BaseModel):
    run_id: UUID
    sequence: int
    type: Literal[
        "status",
        "tool_started",
        "tool_completed",
        "approval_required",
        "clarification_required",
        "response",
        "error",
    ]
    payload: dict
```

Telegram then becomes one consumer. A future web UI, logs viewer or replay tool consumes exactly the same stream.

Even though Telegram cannot render token streaming well, you still benefit from **semantic event streaming**:

- send typing status;
- update “Checking your calendar…”;
- request approval;
- recover from Telegram delivery failure;
- replay the final response from persisted events.

Do not couple your execution engine directly to Telegram messages.

---

## 2. Tool calls begin executing before the full model response finishes

Claude Code reportedly has a `StreamingToolExecutor` that can start tool execution as soon as a complete tool-use block has streamed in, without waiting for the model to finish generating every subsequent block. It also retains a fallback executor that separates tools into concurrent-safe and exclusive groups. citeturn578300view3turn385820view0

That means a model response like:

```text
tool_use: search_calendar(...)
tool_use: search_todoist(...)
text: I’ll compare both sources...
```

can begin both searches before the final text finishes streaming.

A useful execution policy is:

```python
READ_ONLY_PARALLEL = {
    "todoist.search_tasks",
    "calendar.search_events",
    "contacts.search",
}

EXCLUSIVE = {
    "todoist.update_task",
    "calendar.create_event",
    "calendar.delete_event",
}
```

Then:

```python
async def execute_tool_batch(calls):
    parallel = [c for c in calls if is_parallel_safe(c)]
    exclusive = [c for c in calls if not is_parallel_safe(c)]

    parallel_results = await asyncio.gather(
        *(execute(c) for c in parallel),
        return_exceptions=True,
    )

    exclusive_results = []
    for call in exclusive:
        exclusive_results.append(await execute(call))

    return merge_results(calls, parallel_results, exclusive_results)
```

### Takeaway for Jarvis

Add explicit metadata to every tool:

```python
class ToolRuntimePolicy(BaseModel):
    read_only: bool
    idempotent: bool
    parallel_safe: bool
    requires_confirmation: bool
    timeout_seconds: float
    retry_policy: Literal["none", "transient", "idempotent"]
    resource_locks: set[str]
```

For example:

```python
search_todoist.policy = ToolRuntimePolicy(
    read_only=True,
    idempotent=True,
    parallel_safe=True,
    requires_confirmation=False,
    timeout_seconds=8,
    retry_policy="transient",
    resource_locks=set(),
)

update_task.policy = ToolRuntimePolicy(
    read_only=False,
    idempotent=False,
    parallel_safe=False,
    requires_confirmation=True,
    timeout_seconds=10,
    retry_policy="none",
    resource_locks={"todoist:task:{task_id}"},
)
```

This is more valuable than letting the orchestrator vaguely decide whether calls can run concurrently.

---

## 3. The context pipeline is graduated, not “summarize when full”

Claude Code reportedly applies five context-shaping stages before model calls, ordered from cheap and minimally destructive to expensive and lossy:

1. **Budget reduction** — per-message or per-result size caps.
2. **Snip** — trim older history.
3. **Microcompact** — fine-grained compression, reportedly with cache-aware behavior.
4. **Context collapse** — construct a smaller read-time projection without rewriting the underlying transcript.
5. **Auto-compact** — model-generated full summary as the last resort.

Reactive compaction is limited so it does not repeatedly loop during one turn. citeturn578300view3turn578300view4turn385820view0

This is substantially better than:

```python
if tokens > limit:
    conversation = summarize(conversation)
```

### Implement it as deterministic projections

Maintain the canonical transcript unchanged:

```python
canonical_events: list[ConversationEvent]
```

Then create a model-facing projection:

```python
async def build_model_context(events, budget):
    projected = apply_tool_result_caps(events, budget)
    projected = remove_stale_progress_events(projected)
    projected = collapse_redundant_tool_calls(projected)
    projected = replace_old_tool_payloads_with_receipts(projected)

    if estimate_tokens(projected) > budget.soft_limit:
        projected = inject_existing_compaction_summary(projected)

    if estimate_tokens(projected) > budget.hard_limit:
        projected = await generate_compaction_summary(projected)

    return projected
```

For Jarvis, old tool results should generally become receipts:

```json
{
  "type": "tool_receipt",
  "tool": "todoist.search_tasks",
  "called_at": "2026-07-13T11:22:04+08:00",
  "result_count": 18,
  "important_ids": ["task_123", "task_456"],
  "summary": "Found 3 overdue and 15 upcoming tasks.",
  "full_payload_ref": "run-data://..."
}
```

Do not keep all 18 complete Todoist task objects in the LLM context forever.

---

## 4. Compaction does not mutate history

One especially good design is the distinction between:

- **durable canonical history**;
- **the context projected into the next model call**.

Claude Code’s session storage is reportedly append-only JSONL. Compaction boundaries reference identifiers such as a head, anchor and tail, and the loader reconstructs or patches the visible chain at read time. The original log is not destructively rewritten. citeturn578300view5turn385820view0

Conceptually:

```json
{"uuid":"m1","type":"user","content":"..."}
{"uuid":"m2","type":"assistant","content":"..."}
{"uuid":"m3","type":"tool_result","content":"..."}
{"uuid":"c1","type":"compact_boundary",
 "head_uuid":"m1",
 "anchor_uuid":"m3",
 "tail_uuid":"m40",
 "summary":"..."}
```

When resuming:

```python
visible_messages = project_chain(
    events=all_events,
    compact_boundaries=boundaries,
)
```

### Takeaway for Jarvis

Your Supabase schema should distinguish:

```text
runs
run_events
conversation_messages
tool_executions
context_projections
approvals
```

Do not overwrite `conversation_messages` with summaries.

A context projection can record:

```sql
context_projections (
    id uuid,
    run_id uuid,
    model text,
    token_budget integer,
    source_event_start bigint,
    source_event_end bigint,
    summary text,
    included_event_ids jsonb,
    excluded_event_ids jsonb,
    created_at timestamptz
)
```

This gives you:

- full auditability;
- reproducible model inputs;
- easier debugging;
- re-compaction with a better model later;
- accurate backtesting.

---

## 5. User input is persisted synchronously; assistant output can be asynchronous

The leaked implementation reportedly waits for the user message to be durably written before proceeding, but assistant transcript writes may be fire-and-forget. The rationale is very specific: if the process dies immediately after accepting user input but before the API returns, the session must still be resumable from the user’s last accepted request. citeturn578300view8

This is a strong production pattern.

For your Telegram backend:

```python
async def handle_update(update):
    inbound = normalize_telegram_update(update)

    # Must commit before any slow work.
    event_id = await persist_inbound_message(
        telegram_update_id=inbound.update_id,
        chat_id=inbound.chat_id,
        content=inbound.content,
    )

    # Idempotency boundary.
    if await run_already_started(event_id):
        return await replay_existing_outcome(event_id)

    run_id = await create_run(event_id)
    await execute_run(run_id)
```

The acknowledgement boundary should be:

> “The system has durably accepted this message.”

not:

> “The LLM request has started.”

This also helps with Telegram webhook retries.

---

## 6. Permissions are a pipeline, not a Boolean

Claude Code reportedly has seven permission modes and multiple sequential enforcement layers. The important detail is not the mode names. It is the ordering:

1. remove forbidden tools before the model sees them;
2. run pre-tool hooks;
3. evaluate explicit deny/ask/allow rules;
4. route through the relevant permission handler;
5. execute inside applicable sandbox boundaries.

The policy is deny-first: a deny rule beats a more-specific allow rule. citeturn578300view3turn578300view4turn385820view0

### Tool filtering before inference matters

Bad:

```python
tools = all_tools
# Model selects dangerous tool.
# Reject it afterward.
```

Better:

```python
tools = [
    tool for tool in all_tools
    if policy.can_expose(tool, principal, run_context)
]
```

The model cannot call what it cannot see.

### Use a decision object

```python
class PermissionDecision(BaseModel):
    outcome: Literal["allow", "deny", "ask"]
    reason_code: str
    user_message: str | None
    matched_rules: list[str]
    policy_version: str
    risk_level: Literal["low", "medium", "high", "critical"]
```

And evaluate in a strict order:

```python
def decide(call, context):
    if matches_deny_rule(call, context):
        return deny("explicit_deny")

    if not tool_enabled_for_user(call.tool, context.user):
        return deny("tool_not_enabled")

    if call_is_read_only(call):
        return allow("read_only")

    if call_is_reversible(call) and within_safe_scope(call):
        return allow_or_ask_based_on_mode()

    return ask("irreversible_external_effect")
```

### For Jarvis specifically

Your current risky/non-risky confirmation split is too coarse. Distinguish at least:

```text
READ
CREATE_REVERSIBLE
UPDATE_REVERSIBLE
DELETE_RECOVERABLE
EXTERNAL_COMMUNICATION
FINANCIAL_OR_SECURITY_SENSITIVE
```

Examples:

- Search calendar: `READ`
- Create Todoist task: `CREATE_REVERSIBLE`
- Move meeting: `UPDATE_REVERSIBLE`
- Delete calendar event: `DELETE_RECOVERABLE`
- Send email: `EXTERNAL_COMMUNICATION`
- Change OAuth credentials: `SECURITY_SENSITIVE`

Confirmation should depend on **effect and reversibility**, not provider name.

---

## 7. Approval state is deliberately not restored on resume

Claude Code reportedly does not restore elevated permission state when a session resumes. The transcript resumes; the previous trust elevation does not. citeturn578300view3turn578300view5turn385820view0

That is useful for your HITL model.

Persist:

```json
{
  "approval_id": "...",
  "run_id": "...",
  "approved_call_hash": "...",
  "expires_at": "...",
  "status": "approved"
}
```

Do **not** persist:

```json
{
  "user_has_approved_all_calendar_changes_forever": true
}
```

Approval should bind to:

- exact normalized arguments;
- exact tool version;
- run or transaction;
- short expiry;
- intended principal/account.

A robust call hash:

```python
call_hash = sha256(
    canonical_json({
        "tool": tool.name,
        "tool_version": tool.version,
        "arguments": normalized_arguments,
        "principal_id": user.id,
        "connection_id": connection.id,
    })
).hexdigest()
```

If the model changes the time, attendee or task title after approval, the hash changes and approval is invalid.

---

## 8. Tools are assembled dynamically through several filters

Claude Code’s reported tool-pool assembly sequence is:

1. enumerate base tools;
2. filter by execution mode;
3. remove explicitly denied tools;
4. integrate MCP tools;
5. deduplicate.

Up to 54 built-in tools are mentioned for the leaked version. citeturn578300view4turn385820view0

This validates your router → dynamically loaded tool-tip architecture, but I would alter one aspect:

### The router should not be the sole safety or availability boundary

Use it for recall reduction:

```python
candidate_domains = router(user_input)
```

Then independently derive tools:

```python
candidate_tools = registry.tools_for_domains(candidate_domains)

visible_tools = policy_engine.filter_visible(
    candidate_tools,
    user=user,
    connection_state=connections,
    agent_mode=mode,
)
```

Then rank:

```python
visible_tools = semantic_rank(
    query=user_input,
    tools=visible_tools,
    max_tools=12,
)
```

The router can be wrong without creating a security issue. At worst, it omits a needed tool and the orchestrator asks for another pass.

---

## 9. Hooks are treated as first-class control-plane components

The analysis identifies 27 hook events with several execution styles, including shell hooks, LLM-evaluated hooks, webhooks and subagent verification. Hooks can operate at different phases, especially before and after tool execution. citeturn578300view4turn385820view0

For Jarvis, do not hardcode all provider-specific logic into every tool. Define a lifecycle:

```python
async def execute_tool(call, context):
    call = await hooks.run("before_tool_validation", call, context)
    validated = validate_tool_call(call)

    decision = await hooks.run(
        "before_permission_check",
        policy_engine.decide(validated, context),
        context,
    )

    await enforce(decision)

    await hooks.run("before_tool_execute", validated, context)

    try:
        result = await tool.invoke(validated.arguments)
    except Exception as exc:
        await hooks.run("tool_error", exc, context)
        raise

    result = await hooks.run("after_tool_execute", result, context)
    await hooks.run("after_tool_persist", result, context)

    return result
```

Useful hooks in your system:

- redact secrets before LangSmith logging;
- inject idempotency keys;
- enforce per-provider timeouts;
- normalize Todoist and Calendar dates;
- verify a write by reading it back;
- emit Telegram progress events;
- detect suspiciously broad write calls;
- collect latency and token metrics.

Hooks give you cross-cutting behavior without contaminating the orchestrator prompt.

---

## 10. Skills and subagents are deliberately different primitives

A skill injects instructions into the current context. A subagent receives a new, isolated context. The reported implementation calls these roughly `SkillTool` and `AgentTool`.

The distinction:

```text
Skill:
- same conversation
- same context window
- cheap
- good for compact procedural knowledge

Subagent:
- separate context
- separate transcript
- much more expensive
- protects parent context
- returns only a summary
```

The analysis estimates isolated agents may consume around seven times the tokens of skill injection in some paths, though this figure should be treated as implementation-specific rather than universal. citeturn578300view4turn578300view5turn385820view0

### Implication for your router/orchestrator design

Do not use decomposition automatically merely because a request has multiple steps.

Use a skill/instruction injection when:

- it is one domain;
- tool results are small;
- the task is sequential;
- the same orchestrator can retain all relevant context.

Use a subagent when:

- exploration generates lots of intermediate material;
- domains can operate independently;
- you need adversarial verification;
- a task requires different tools or permission scopes;
- you want to protect the parent context.

For example:

> “Look at my Todoist and Google Calendar, find conflicts next week, then propose rescheduling options.”

This does not inherently require two agents. Parallel tool calls in one context are likely cheaper.

But:

> “Research five hosting platforms, inspect my GitHub architecture, estimate costs, and recommend a migration plan.”

Separate research and repository-analysis agents may be justified because each produces large intermediate evidence.

---

## 11. Subagent histories are sidechains; only summaries enter the parent

Each subagent reportedly writes its own JSONL transcript. The parent receives a compact result rather than the entire child trajectory. citeturn578300view5turn385820view0

A good result contract is:

```python
class SubagentResult(BaseModel):
    status: Literal["completed", "partial", "failed"]
    conclusion: str
    evidence: list[EvidenceRef]
    artifacts: list[ArtifactRef]
    unresolved_questions: list[str]
    tool_receipts: list[ToolReceipt]
    confidence: float
    sidechain_id: UUID
```

Avoid:

```python
parent.messages.extend(child.messages)
```

Use:

```python
parent.messages.append(
    Message(
        role="user",
        content=render_subagent_summary(result),
    )
)
```

For your planned DAG, this means each node should return a bounded structured result, not dump arbitrary conversational history into the reducer.

---

## 12. File-system isolation is a selectable subagent policy

Claude Code reportedly supports three forms of subagent isolation:

- in-process with isolated conversation;
- Git worktree isolation;
- remote isolation.

Coordination between local instances reportedly uses POSIX `flock()`, avoiding a separate distributed lock service for local operations. citeturn578300view5turn385820view0

For a personal assistant, the analogous resource-isolation concept is **connection and entity locking**:

```text
calendar:event:abc123
todoist:task:def456
gmail:thread:ghi789
user:123:conversation
```

Before mutating:

```python
async with lock_manager.acquire(
    key=f"calendar:event:{event_id}",
    ttl=30,
):
    latest = await calendar.get_event(event_id)
    verify_preconditions(latest, expected_etag)
    return await calendar.update_event(...)
```

This prevents two concurrent agent runs from rescheduling the same event based on stale state.

Use database advisory locks or a `locked_until` transaction initially. You probably do not need Redis at ten users.

---

## 13. Memory is inspectable and file-based rather than vector-first

The reported memory mechanism does not default to a vector database. It scans memory-file headers using an LLM and selects up to five relevant files. Project/user/local instruction files have explicit precedence. citeturn578300view4turn385820view0

The deeper takeaway is not “vector DBs are bad.” It is:

> At small scale, explicit namespaces and concise descriptions can outperform an opaque retrieval stack operationally.

For Jarvis, memory records could be:

```yaml
id: calendar-preferences
scope: user
description: >
  Preferences governing which calendar provider to use,
  default durations, WFO/WFH handling, and confirmation behavior.
updated_at: 2026-07-10
content: |
  Todoist is the default scheduling source.
  Google Calendar is explicit-only unless...
```

Selection:

```python
headers = await memory_store.list_headers(user_id)

selected_ids = await small_model.select(
    query=current_request,
    headers=headers,
    max_items=5,
)

memories = await memory_store.fetch(selected_ids)
```

This is simpler to debug than embedding every past conversation.

Use embeddings later for high-volume episodic recall, not for ten carefully maintained preference files.

---

## 14. The system prompt is split at a cache boundary

The analysis reports a static prompt prefix and dynamic session suffix, with the cacheable prefix hashed and reused. Roughly 3,000 static instruction tokens were reportedly globally cacheable in the examined version. citeturn578300view7

Your current dynamic tool-tip loading should follow the same principle:

```text
CACHEABLE PREFIX
- agent identity
- universal behavior
- output contracts
- generic tool-calling rules
- safety invariants
- schema definitions

DYNAMIC SUFFIX
- user input
- current date/time
- connection status
- selected domain tips
- visible tools
- recent history
- retrieved memory
```

Do not inject frequently changing details early in the prompt. A timestamp near the top can invalidate a large cacheable prefix.

Also keep tool schemas stable:

- deterministic ordering;
- canonical JSON;
- stable description wording;
- dynamic values outside schema text where possible.

This can matter significantly for your input-heavy router/orchestrator economics.

---

## 15. Latency is hidden through overlapping work

Two reported techniques are particularly relevant:

### Memory prefetch

While the model is streaming, relevant memory retrieval begins concurrently so it is ready before later processing. Resource cleanup is attached to all generator exit paths. citeturn578300view9

### Speculative computation

The implementation reportedly has paths that precompute likely next responses for predictable interactions such as confirmations. This is a more speculative community interpretation and should not be treated as a guaranteed description of all Claude Code behavior. citeturn578300view9

For Jarvis, the safer optimizations are:

```python
async with asyncio.TaskGroup() as tg:
    connection_task = tg.create_task(load_connections(user_id))
    memory_task = tg.create_task(retrieve_memory(message))
    conversation_task = tg.create_task(load_recent_context(chat_id))
    timezone_task = tg.create_task(resolve_timezone(user_id))
```

Then start provider calls as soon as their arguments are fully known.

Do not precompute expensive LLM answers while a user is merely typing in Telegram; you do not receive useful continuous typing content anyway. But you can prefetch:

- OAuth connection state;
- user preferences;
- recent task/calendar cache;
- tool registry and policy.

---

## 16. Recovery is designed as explicit stages

The reported runtime includes:

- output-token escalation with limited retries;
- reactive compaction at most once per turn;
- prompt-too-long recovery;
- streaming fallback;
- fallback model switching;
- clear stop conditions such as no tool calls, max turns, overflow, hook stop and explicit abort. citeturn578300view3turn385820view0

Your run state should encode why it stopped:

```python
StopReason = Literal[
    "completed",
    "no_tool_calls",
    "max_turns",
    "context_overflow",
    "user_abort",
    "approval_required",
    "clarification_required",
    "policy_denied",
    "provider_unavailable",
    "fatal_error",
]
```

And retries need typed causes:

```python
class RetryDecision(BaseModel):
    retry: bool
    strategy: Literal[
        "same_request",
        "compact_context",
        "increase_output_limit",
        "switch_model",
        "refresh_token",
        "wait_backoff",
    ]
    max_attempts: int
```

Avoid generic:

```python
except Exception:
    retry()
```

For writes, retries must require either idempotency or post-failure reconciliation.

---

## 17. Verification is a separate concern from generation

The leaked code reportedly contained a feature-gated verification agent for adversarial review of non-trivial changes, along with explicit anti-hallucination instructions such as not claiming tests passed when outputs show failures. citeturn578300view7

For your assistant, verification should be deterministic before it becomes another LLM.

After a calendar write:

```python
created = await calendar.create_event(payload)
fetched = await calendar.get_event(created.id)

assert normalize(fetched.start) == normalize(payload.start)
assert normalize(fetched.end) == normalize(payload.end)
assert fetched.summary == payload.summary
```

After a Todoist update:

```python
updated = await todoist.update_task(...)
verified = await todoist.get_task(task_id)

if verified.due != intended_due:
    raise VerificationError(...)
```

Only use an LLM verifier for semantic questions:

- Did the final answer accurately represent all tool results?
- Did it omit an unresolved conflict?
- Does the proposed schedule satisfy the user’s natural-language constraints?

This suggests a final node:

```text
agent → tools → deterministic verification
      → optional semantic verifier
      → final response
```

---

## 18. Build-time feature flags create multiple products from one codebase

The source reportedly uses compile-time feature flags and dead-code elimination so internal, public CLI, SDK and experimental builds can include different modules without runtime cost. citeturn578300view7turn578300view8

For your backend, you likely need runtime flags rather than compile-time flags, but separate these categories:

```text
release flag:
  Is the feature visible to users?

capability flag:
  Is the integration/tool installed?

policy flag:
  Is the user allowed to use it?

experiment flag:
  Which behavior variant is selected?

kill switch:
  Must execution stop immediately?
```

Do not use one `FEATURE_ENABLED` Boolean for all five concepts.

Example:

```python
if not capability.todoist:
    exclude_tools("todoist.*")

if kill_switch.calendar_writes:
    deny_tools("calendar.create", "calendar.update", "calendar.delete")

variant = experiments.assign("router_payload_v2", user_id)
```

---

# Less flattering lessons from the leak

## Security initialization order matters

The architectural analysis reports vulnerabilities caused by extensions, hooks or MCP-related initialization occurring before the repository trust boundary was fully established. That creates a “pre-trust execution window.” citeturn578300view0turn578300view4turn385820view0

For your system:

```text
Bad startup:
1. Load user-supplied plugin
2. Execute plugin initialization
3. Check whether plugin is trusted
```

Required:

```text
1. Parse metadata without execution
2. Resolve owner and signature
3. Apply trust policy
4. Build restricted environment
5. Execute initialization
```

Never let remote MCP metadata, tool descriptions, OAuth provider responses or webhook payloads become trusted prompt instructions without delimiting and sanitizing them.

---

## A second LLM classifier is not an independent security boundary

Claude Code’s auto permission mode reportedly calls a separate classifier model. But if both the orchestrator and classifier share similar model vulnerabilities, prompt-injection exposure or token-cost constraints, this is not truly independent defense in depth. The community analysis also notes that some large shell-command patterns could bypass expensive analysis, illustrating shared failure modes. citeturn578300view4turn385820view1

For Jarvis:

- deterministic policy first;
- sandbox or scope restriction second;
- confirmation third;
- LLM risk classification only as a supplementary signal.

Never let the classifier override an explicit deny rule.

---

## Approval fatigue is real

The research summary cites a very high approval rate for prompts, suggesting users often approve automatically. The authors’ conclusion is that the solution is safer autonomous zones, not more pop-ups. citeturn578300view2turn385820view1

For your Telegram assistant, do not require confirmation for every reversible task creation. Consider:

```text
Auto:
- read operations
- create a Todoist task
- mark task complete, with undo
- create personal calendar placeholder, where user opted in

Ask:
- delete
- message another person
- modify meetings with attendees
- bulk changes
- ambiguous target
- external side effects
```

Also support a compact confirmation:

```text
Create 8 Todoist tasks in “Korea Trip” due 18 July?
[Confirm] [Edit] [Cancel]
```

Do not show eight separate prompts unless the calls materially differ in risk.

---

# What I would change in your Jarvis architecture now

In priority order:

## P0 — Durable run ledger

Add append-oriented `run_events` and synchronously persist the incoming Telegram message before model or tool work begins.

```text
telegram update
  → persist inbound event
  → create run
  → execute
  → persist each transition
  → deliver/replay output
```

## P0 — Tool runtime metadata

Give every tool deterministic metadata for:

- read/write;
- reversibility;
- parallel safety;
- idempotency;
- retry class;
- confirmation;
- resource locks;
- timeout.

Your orchestrator should not infer these properties from prose.

## P0 — Exact-call approvals

Bind confirmation to a canonical call hash and expiration. Never resume a broad permission mode after process restart.

## P1 — Unified event stream

Have LangGraph nodes emit one normalized event model. Telegram should be a renderer/transport adapter, not embedded in agent logic.

## P1 — Parallel read executor

Execute Todoist and Calendar reads concurrently when independent. Serialize writes by resource lock.

## P1 — Context projections

Keep canonical history immutable. Generate bounded model-facing projections with tool receipts and staged compaction.

## P1 — Read-after-write verification

Verify provider mutations before telling the user they succeeded.

## P2 — Skill versus subagent distinction

Keep compact domain instructions in the main context. Use subagents only for context-heavy exploration or independent verification.

## P2 — Hook lifecycle

Introduce pre-validation, pre-permission, pre-execution, post-execution, error and final-response hooks.

## P2 — Prompt-cache-aware assembly

Keep static rules and schemas before dynamic state. Canonicalize tool ordering and descriptions.

---

# A concrete target runtime

```python
async def run_agent(run_id: UUID) -> None:
    run = await runs.load(run_id)
    await events.emit(run_id, "run_started", {})

    async with asyncio.TaskGroup() as tg:
        user_context_t = tg.create_task(load_user_context(run.user_id))
        memory_t = tg.create_task(prefetch_memories(run))
        connection_t = tg.create_task(load_connection_state(run.user_id))

    domains = await router.route(
        message=run.user_message,
        connections=connection_t.result(),
    )

    tools = tool_registry.for_domains(domains)
    tools = policy.filter_visible_tools(
        tools=tools,
        user_context=user_context_t.result(),
    )

    state = await context_builder.build(
        run=run,
        memories=memory_t.result(),
        tools=tools,
    )

    for turn in range(MAX_TURNS):
        projection = await compactor.project(state)

        async for event in model.stream(projection):
            await events.persist(run_id, event)

            if event.type == "tool_call_complete":
                call = normalize_tool_call(event)

                decision = await permission_engine.evaluate(call, state)

                if decision.outcome == "deny":
                    state.add_tool_denial(call, decision)
                    continue

                if decision.outcome == "ask":
                    await approvals.create(run_id, call, decision)
                    await runs.pause(run_id, "approval_required")
                    return

                result = await executor.execute(
                    call,
                    policy=tool_registry.policy(call.tool),
                )

                verification = await verifier.verify(call, result)
                state.add_tool_result(call, result, verification)

        if state.has_final_response:
            await delivery.send_or_replay(state.final_response)
            await runs.complete(run_id)
            return

    await runs.fail(run_id, reason="max_turns")
```

The central architectural lesson is:

> Keep the model responsible for interpreting intent and choosing useful actions. Move concurrency, authorization, persistence, retries, idempotency, compaction, verification and lifecycle control into deterministic code.

That is the most transferable low-level takeaway from Claude Code. The leaked implementation is not mainly a clever planner. It is a large collection of production scars encoded around a relatively ordinary model/tool loop. citeturn578300view0turn578300view3turn578300view9