Main architectural takeaway for Jarvis

Hermes is roughly:

Universal gateway
    +
Large central synchronous agent loop
    +
Prompt-based skills
    +
Central tool registry
    +
SQLite sessions
    +
Pluggable memory/hooks/providers
    +
Threaded tool concurrency

Your current Jarvis architecture is more explicitly decomposed:

Telegram layer
    ↓
Router
    ↓
LangGraph orchestrator
    ↓
Tool nodes
    ↓
HITL confirmation / clarification
    ↓
Final response

The best Hermes ideas to borrow are:

One normalized internal message format across model providers.
Tools that self-register with schema, handler and availability metadata.
Skills as procedural context, separate from executable tools.
Lifecycle hooks around LLM calls, tools and final-answer verification.
Session search separated from semantic memory.
A fresh-agent execution model for scheduled jobs.
Shared callbacks allowing Telegram, CLI and other interfaces to consume the same agent-progress events.


## Hooks:
Think of the hooks as **interception points around one user turn**:

```text
User message
   ↓
pre_llm_call
   ↓
LLM thinks → requests tools
   ↓
pre_tool_call
   ↓
Tool executes
   ↓
post_tool_call
   ↓
LLM produces proposed final answer
   ↓
pre_verify
   ↓
post_llm_call
   ↓
Answer delivered

...later, when conversation/session closes...

on_session_end
```

## `pre_llm_call`

Runs **before the model begins the turn**.

### Example: inject relevant project context

User asks:

> Why is the deployment failing?

The hook searches your internal incident database and injects:

```text
Additional context:
- The latest deployment failed after upgrading pydantic to v2.
- The repository still uses several v1 validators.
```

Conceptually:

```python
def pre_llm_call(user_message, conversation_history, **kwargs):
    context = search_internal_docs(user_message)

    if context:
        return f"""
Relevant internal context:
{context}
"""
```

The model now receives that context alongside the user message.

Good uses:

* Retrieval-augmented generation
* Injecting user preferences
* Adding current repository state
* Adding platform-specific context
* Loading recent related memories

In Hermes, this is one of the hooks whose return value can actively inject context; most other lifecycle hooks are primarily observers. ([GitHub][1])

---

## `pre_tool_call`

Runs **after the model requests a tool but before it executes**.

### Example: block a destructive command

The model requests:

```json
{
  "tool": "terminal",
  "arguments": {
    "command": "rm -rf /"
  }
}
```

The hook checks the call:

```python
def pre_tool_call(tool_name, args, **kwargs):
    if tool_name == "terminal":
        command = args.get("command", "")

        if "rm -rf /" in command:
            return {
                "block": True,
                "reason": "Blocked destructive filesystem command."
            }
```

Hermes returns the block reason to the model instead of running the command.

Another example for your Jarvis agent:

```python
def pre_tool_call(tool_name, args, **kwargs):
    if tool_name == "delete_todoist_task":
        return {
            "block": True,
            "reason": "User confirmation is required before deleting tasks."
        }
```

Good uses:

* Safety approval gates
* Permission checking
* Tool allowlists
* Argument validation
* Rate limiting
* Idempotency checks
* Requiring confirmation

Hermes explicitly supports using `pre_tool_call` to block tool execution. ([GitHub][2])

---

## `post_tool_call`

Runs **after the tool has executed**, with access to the result.

### Example: log calendar mutations

Suppose the model calls:

```text
create_calendar_event(
    title="G10 rates shadowing",
    start="2026-07-15 09:00"
)
```

The hook records an audit event:

```python
def post_tool_call(tool_name, args, result, **kwargs):
    if tool_name == "create_calendar_event":
        audit_log.write({
            "action": tool_name,
            "arguments": args,
            "result": result,
            "timestamp": now(),
        })
```

### Example: automatically format edited code

```python
def post_tool_call(tool_name, args, result, **kwargs):
    if tool_name == "write_file":
        path = args.get("path", "")

        if path.endswith(".py"):
            subprocess.run(["ruff", "format", path])
```

Good uses:

* Audit logging
* Metrics and latency tracking
* Formatting modified files
* Invalidating caches
* Updating external state
* Detecting suspicious tool results
* Recording successful mutations

Usually, this hook observes the result rather than changing whether the tool ran.

---

## `post_llm_call`

Runs **after the full LLM/tool loop has successfully produced an answer**.

It is generally not called after every individual model invocation inside the tool loop. It applies once the completed turn has an assistant response. ([GitHub][3])

### Example: record response metrics

```python
def post_llm_call(
    user_message,
    assistant_response,
    model,
    conversation_history,
    **kwargs,
):
    analytics.record({
        "model": model,
        "input": user_message,
        "response_length": len(assistant_response),
        "tool_calls": count_tool_calls(conversation_history),
    })
```

### Example: asynchronously update emotion/persona state

```python
def post_llm_call(user_message, assistant_response, **kwargs):
    detected_state = classify_conversation_state(
        user_message,
        assistant_response,
    )

    persona_store.update(detected_state)
```

Good uses:

* Logging complete turns
* Cost and latency analytics
* Quality evaluation
* Conversation-state updates
* Feedback collection
* Triggering external notifications
* Updating a user model

The resulting answer has already been produced, so this is better for observation or downstream updates than safety enforcement.

---

## `pre_verify`

Runs when the agent is **about to accept its proposed answer as finished**, particularly after code or file modifications.

Unlike ordinary observer hooks, it may tell the agent:

> You are not actually done yet. Perform this check and continue for another iteration.

### Example: require tests after editing code

The model edits `main.rs` and tries to answer:

> Done—the formatting issue has been fixed.

The hook checks the turn and notices no tests were run:

```python
def pre_verify(
    assistant_response,
    tools_used,
    files_modified,
    **kwargs,
):
    edited_code = bool(files_modified)
    ran_tests = any(
        call.tool == "terminal"
        and "cargo test" in call.args.get("command", "")
        for call in tools_used
    )

    if edited_code and not ran_tests:
        return "Run `cargo test` and verify that the project compiles before finishing."
```

Hermes injects a synthetic continuation instruction, so the agent loops again:

```text
Agent proposed final answer
    ↓
pre_verify rejects completion
    ↓
Synthetic user message:
"Run cargo test and verify compilation before finishing."
    ↓
Agent calls terminal
    ↓
Agent produces a new final answer
```

Other examples:

* Require `ruff check` after Python edits
* Require `cargo check` after Rust edits
* Require the agent to reread a modified file
* Check that claimed output files really exist
* Require sources before completing research
* Ensure a deployment health check passed

Hermes bounds these verification nudges so a broken hook cannot continue the agent forever. Its built-in verification guidance is separate from custom `pre_verify` hooks. ([GitHub][1])

---

## `on_session_end`

Runs when the **conversation session is being closed, expired, reset or finalized**, rather than after every turn.

### Example: extract durable memory

Conversation history contains:

```text
User: For my Jarvis agent, Todoist should remain the default.
User: Only use Google Calendar when I explicitly say Google Calendar.
```

When the session ends:

```python
def on_session_end(session_id, conversation_history, **kwargs):
    memories = extract_long_term_facts(conversation_history)

    memory_store.save(
        session_id=session_id,
        memories=memories,
    )
```

Stored memory:

```text
- Todoist is the user's default task and scheduling provider.
- Google Calendar should only be used when explicitly requested.
```

### Example: final session summary

```python
def on_session_end(session_id, conversation_history, **kwargs):
    summary = summarize_session(conversation_history)

    database.save_session_summary(
        session_id=session_id,
        summary=summary,
    )
```

Good uses:

* Final memory extraction
* Persisting a session summary
* Flushing buffered traces
* Closing database or network resources
* Saving unfinished tasks
* Cleaning temporary files
* Updating long-term user profiles

Hermes’s documented memory lifecycle uses session end/reset as a final opportunity to flush or extract persistent information. ([GitHub][4])

## Applied to your Jarvis architecture

A sensible mapping would be:

| Hook             | Jarvis example                                                              |
| ---------------- | --------------------------------------------------------------------------- |
| `pre_llm_call`   | Inject router output, relevant memories and provider-specific tool guidance |
| `pre_tool_call`  | Risk classification, confirmation gate and idempotency validation           |
| `post_tool_call` | Log mutation result and verify Todoist/Calendar actually changed            |
| `post_llm_call`  | Store latency, token cost, domains selected and final-answer quality        |
| `pre_verify`     | Prevent “done” until requested mutations are read back and confirmed        |
| `on_session_end` | Summarize the thread and extract durable user preferences                   |

The most valuable two for Jarvis are probably **`pre_tool_call`**, for your HITL and safety gate, and **`pre_verify`**, because it prevents the assistant from claiming success merely because a tool returned without throwing an exception.

[1]: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md?utm_source=chatgpt.com "hermes-agent/website/docs/user-guide/features/hooks.md ..."
[2]: https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/plugins.py?utm_source=chatgpt.com "hermes-agent/hermes_cli/plugins.py at main"
[3]: https://github.com/NousResearch/hermes-agent/issues/15040?utm_source=chatgpt.com "[i18n] Thai Translation: Guides Part a - automate-with-cron ..."
[4]: https://github.com/NousResearch/hermes-agent/issues/11205?utm_source=chatgpt.com "[Bug]: MemoryProvider.on_session_end() never called on ..."
