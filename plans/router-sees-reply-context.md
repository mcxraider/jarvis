# Plan: Feed reply-to context into the domain router

## Problem

When a user replies to a Telegram message (e.g. replies to a calendar-event message with "move this to 3pm"), the replied-to text reaches the **orchestrator LLM** but **not the domain router / tool-selector**.

The router classifies only the bare `user_prompt` ("move this to 3pm"), so it can pick the wrong domain or fall back to all-tools. The orchestrator later sees the reply and reasons correctly — but by then the router has already narrowed which domain tools are exposed, so on a clean router hit the needed tools may be absent.

## Root cause

Reply context splits into two lanes at the Python agent:

- **Orchestrator LLM (gets it):** `build_user_prompt_with_request_datetime` injects `reply_context` into the user message — `agents/agent_api/app/graph/prompts/context.py:327`.
- **Router / tool-selector (does NOT get it):** `orchestrator.py:1023-1038` builds `routing_query` from `state["user_prompt"]` only.

`reply_context` is passed to `build_initial_state` (`builder.py:408`) but only forwarded to `build_initial_messages` — it is **never stored in `JarvisState`**, so the orchestrator node has no way to read it for routing.

## Fix (smallest change that works)

Thread `reply_context` into state, then fold it into `routing_query` the same way clarification history already is.

### 1. `agents/agent_api/app/graph/state.py`
Add one field to `JarvisState`:
```python
reply_context: Dict[str, Any]
```

### 2. `agents/agent_api/app/graph/builder.py` (`build_initial_state`, ~line 425)
Persist the value already being received:
```python
"user_prompt": user_prompt,
"reply_context": reply_context,   # new
```

### 3. `agents/agent_api/app/graph/nodes/orchestrator.py` (~line 1032-1038)
After the existing clarification-history branch, append reply context to the routing query. Clarification history takes priority (it's the latest intent signal); reply context augments the base prompt when there's no clarification:
```python
else:
    routing_query = user_prompt

reply_context = state.get("reply_context")
if reply_context and reply_context.get("message"):
    routing_query = f"{routing_query} [Replied to: {reply_context['message']}]"
```
Only `routing_query` changes — `user_prompt` and the message list are untouched, so orchestrator behavior is unchanged.

## Notes / scope

- Router-only change: the orchestrator LLM already had reply context, so no double-injection.
- Resume turns: `reply_context` persists in the checkpointed state, so HITL resumes keep it for re-routing (consistent with how `active_domains` is preserved).
- Static/keyword selectors ignore the extra text (static = pass-through; keyword may match more, which is harmless). Only the LLM `RouterClient` meaningfully benefits.

## Check

One Python test in `tests/agents/` (near the existing router/selection tests): build a state with `reply_context` pointing at a calendar message + a bare `user_prompt` that names no domain, assert the router selector receives the reply text in its query (or that calendar tools end up selected). Fails today, passes after the change.
