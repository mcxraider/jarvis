# Design: "Final reply to user" node

Last updated: 2026-06-29
Status: Proposal / design doc (not yet implemented)

## Context

Today the LangGraph agent produces the text that gets sent to the user **as a
side effect of the `agent` node**. When DeepSeek returns a message with no tool
calls, the orchestrator copies the raw model content into state and exits:

```python
# agents/agent_api/app/graph/nodes/orchestrator.py  (~line 411-436)
if not assistant_message.get("tool_calls"):
    content = assistant_message.get("content") or ""
    final_response = content  # <-- raw model text becomes the user reply
    next_node = "end"
```

Question-like text is not upgraded automatically. The model must issue an
explicit `ask_user` tool call when it needs a reply.

`to_response()` then packages `final_response` verbatim into the API response,
and the TypeScript service renders it to Telegram (MarkdownV2, splitting, rich
messages — `src/services/telegram/formatters/`).

This has three structural weaknesses:

1. **No formatting guarantee.** The reply is exactly whatever the model emitted.
   The orchestrator prompt *asks* for clean GFM and "no Telegram tags", but
   nothing enforces it. Code-fence-wrapped answers, stray HTML, empty strings,
   trailing "let me know if you need anything else" filler, and over-long
   answers all pass straight through.
2. **No completion check.** The model can *say* "Done — I created your 3 tasks"
   while the underlying tool results show a failure, a `mutation_blocked`, a
   partial batch failure, or no create call at all. Nothing reconciles the
   **claim** against the **structured `tool_results` envelopes** already sitting
   in state. The user can be told something succeeded when it did not.
3. **No single place to own the user-facing reply.** Five+ code paths set
   `final_response` (answer, max-turns, LLM failure, confirm-decline, …). There
   is no choke point for formatting, verification, receipts, or metrics.

This document proposes a dedicated **`finalize`** node ("Final reply to user")
that becomes the single owner of the user-facing answer: it formats the draft,
verifies that the work actually happened, and can bounce a dishonest/incomplete
draft back to the agent for correction before anything reaches the user.

## Goals

- Make every successful answer pass through one node before `end`.
- Reconcile the model's claim against `state["tool_results"]` and refuse to ship
  hallucinated success.
- Normalize formatting deterministically (GFM cleanup, length budget, empty-reply
  guard) without fighting the TS-side MarkdownV2 layer.
- Be cheap by default (no extra LLM call) with an optional LLM verifier for
  semantic checks.
- Add an observable verdict for tracing/metrics.

## Non-goals

- Telegram-specific rendering. That stays in TS
  (`telegram-markdown.ts`, `telegram-rich.ts`, `message-splitter.ts`). The node
  emits **GitHub-Flavored Markdown only**.
- Replacing the HITL / confirm interrupt flows. Those keep setting their own
  user-facing text and route straight to `end` (see "Which paths go through
  finalize").

---

## Responsibilities of the node

### 1. Formatting (deterministic, always on)

A pure-Python normalization pass over the draft — no model call. It should:

- **Empty-reply guard.** If the draft is empty/whitespace (currently this leaks
  out as `response: ""`), substitute a safe fallback derived from the tool
  receipts, or `"Done."` when a mutation succeeded with nothing else to say.
- **Unwrap full code-fence answers.** If the *entire* reply is wrapped in a
  ```` ``` ```` block (a common model tic the prompt forbids), strip the fence.
- **Strip disallowed markup.** Remove HTML tags and any Telegram-specific tags
  (`<tg-*>`), which the prompt forbids but does not enforce.
- **Trim filler tails.** Drop trailing "Let me know if you need anything else /
  happy to help / feel free to ask" sentences. (The orchestrator prompt already
  says not to append these; this is the enforcement.)
- **Collapse whitespace.** Normalize 3+ blank lines to a single blank line; strip
  leading/trailing whitespace.
- **Length budget.** Soft-cap very long replies (the TS splitter handles the hard
  4096 Telegram limit, but the node can cap absurd outputs and add a
  "…truncated" marker so the model can't dump 50k chars).

Keep this in a small, unit-testable helper, e.g.
`format_final_reply(draft: str, receipts: Receipts) -> str`.

### 2. Completion verification (the core value)

Cross-check the draft and the model's intent against the **canonical tool-result
envelopes** already accumulated in `state["tool_results"]`. Each envelope
(`agents/agent_api/app/tools/dispatcher.py:build_tool_result`) carries exactly
the signals needed:

```python
{ "tool_call_id", "tool_name", "success", "content",
  "error", "mutation_blocked", "classified_error" }
```

Deterministic checks (no LLM needed):

- **Failed tool calls.** Any envelope with `success is False` →
  the work is at least partially incomplete.
- **Blocked mutations.** Any envelope with `mutation_blocked is True`
  (the `ALLOW_MUTATIONS` guard fired) → nothing was actually written.
- **Classified errors.** `classified_error` present → surfaceable API failure.
- **Decline mismatch.** `state["confirm_decision"] == "decline"` while the draft
  claims the action was performed.
- **Claim-vs-action mismatch.** The draft asserts a mutation
  ("created", "deleted", "completed", "moved", "rescheduled") but no *successful*
  mutating tool (`create_task`, `close_task`, `delete_task`, `update_task`, …)
  appears in `tool_results`. This catches the "I created it" hallucination when
  the model never called the tool or the call failed.

Produce a structured verdict:

```python
class Verification(TypedDict):
    ok: bool
    issues: list[str]        # human-readable, e.g. "create_task failed: 403"
    failed_tools: list[str]
    blocked: bool
    severity: Literal["none", "soft", "hard"]
```

- **`none`** → ship the formatted draft.
- **`soft`** (e.g. minor: read tool returned empty, cosmetic) → ship, optionally
  with an appended note.
- **`hard`** (claimed success but a mutation failed / was blocked / declined) →
  do **not** ship the draft. Route back to the agent for correction (see node 4).

### 3. Receipts / audit summary (deterministic, optional appendix)

Derive a concise, trustworthy "what actually happened" line **from the
envelopes, not the model's prose**, e.g.:

```
✓ Created 2 tasks · Completed 1 · 1 failed (see above)
```

Because it is computed from `tool_results`, it cannot lie. This is the cheapest
high-value add and directly serves "checking if tasks are actually complete."
Gate it behind a flag — for simple read queries the receipt is noise; for
multi-mutation batches it is reassuring.

### 4. Verify-and-correct loop (the differentiator)

When verification severity is `hard`, instead of sending a wrong answer, append
a system message to the conversation describing the discrepancy and route back
to `agent`:

```
[verification] Your answer claimed the task was created, but create_task
returned success=false (error: project not found). Do not claim success.
Either correct the failure with another tool call or tell the user honestly
what failed.
```

The agent gets one more turn to fix the work or write an honest reply. This
turns the finalize node from a passive formatter into an **active honesty gate**.
Guard it with a counter so it cannot loop forever (see state changes).

### 5. Other candidate responsibilities (call out as decisions)

- **Next-step suggestions.** Proactively offer a sensible follow-up
  ("Want me to set a due date?"). **Conflicts** with the current orchestrator
  prompt rule ("do not append offers for further help"). Flagged as Open
  Decision D3 — do not add silently.
- **Persona/tone pass.** Enforce the "Jarvis" voice. Needs an LLM call; low ROI
  vs. cost. Defer.
- **Observability.** Always emit a trace event
  (`graph.finalize` with the verdict) for LangSmith and run-log files, mirroring
  the existing `tracer.event(...)` pattern in other nodes.

---

## Architecture & integration

### Graph wiring

Add one `NodeSpec` and rewire the agent's ANSWER edge. Today:

```
agent --(no tool calls, not a question)--> END
```

Proposed:

```
agent --(answer)--> finalize --(ok)--------> END
                          \--(hard fail)--> agent   (bounded retry)
```

In `agents/agent_api/app/graph/builder.py`, register alongside the existing
specs (mirrors `create_summarize_node` which is a simple single-purpose node):

```python
NodeSpec(
    name="finalize",
    node=create_finalize_node(deepseek_client, tracer),  # client optional
    router=route_after_finalize,
    route_map={"end": "end", "agent": "agent"},
),
```

And change the `agent` node's route map so the answer branch targets `finalize`
instead of `end`:

```python
NodeSpec(
    name="agent",
    node=create_agent_node(...),
    router=route_after_agent,
    route_map={"hitl": "hitl", "validate": "validate_entities",
               "finalize": "finalize", "end": "end"},   # +finalize
),
```

### Orchestrator change

In `orchestrator.py`, the ANSWER branch stops writing `final_response` and
instead writes a **draft** and routes to `finalize`:

```python
else:
    return {
        "messages": messages,
        "turn_count": turn_count + 1,
        "draft_response": content,   # was: final_response = content
        "next": "finalize",          # was: "end"
    }
```

`finalize` reads `draft_response`, runs the format + verify pipeline, and writes
the real `final_response`.

### Which paths go through finalize

Only the **normal successful answer** path. The other `final_response` setters
are already honest, terminal strings and should bypass finalize to avoid extra
work and accidental rewrites:

| Path | Source | Goes through finalize? |
|------|--------|------------------------|
| Model answers, no tool calls | orchestrator answer branch | **Yes** |
| Max turns exceeded | orchestrator | No — route `end` |
| DeepSeek failure | orchestrator | No — route `end` |
| Confirm declined | confirm node | No — route `end` |
| Interrupt (clarify/confirm) | hitl/confirm | No — interrupt payload, not final_response |

(If we later want format-only normalization on the error strings too, finalize
can expose a `format_only=True` fast path. Not needed for v1.)

### State changes

In `agents/agent_api/app/graph/state.py` (`JarvisState`):

```python
draft_response: str            # model's pre-finalize answer
verification: Dict[str, Any]   # the Verification verdict (for tracing/response)
finalize_attempts: int         # guards the verify->agent retry loop
```

`route_after_finalize` logic:

```python
def route_after_finalize(state) -> str:
    v = state.get("verification", {})
    attempts = state.get("finalize_attempts", 0)
    if v.get("severity") == "hard" and attempts < MAX_FINALIZE_RETRIES:  # e.g. 1
        return "agent"
    return "end"
```

When the retry budget is exhausted but verification still fails `hard`, finalize
ships an **honest** reply (draft rewritten to disclose the failure using the
receipts) rather than the model's optimistic claim.

### LLM vs deterministic — recommended split

| Approach | Latency/cost | Catches | Recommendation |
|----------|--------------|---------|----------------|
| Deterministic only | ~0 | structural failures, blocks, declines, claim/action mismatch, formatting | **v1 baseline** |
| + LLM verifier | +1 DeepSeek call | semantic "does the prose match what the tools returned" | v2, gated/optional |
| + LLM rewrite/suggestions | +1 call | tone, follow-ups | defer |

Start deterministic. The structured envelopes already encode success/failure, so
most of the value needs no model call. Add the LLM verifier behind a config flag
(`FINALIZE_LLM_VERIFY=false` default) for ambiguous cases only, reusing the
existing `DeepSeekAgentClient` with a tight JSON-returning prompt
(`reasoning_effort` low, no tools).

---

## Progress / UX

Add a streaming stage so the user sees the step. Python emits a progress event
(same mechanism the orchestrator uses) and TS maps it in
`src/services/telegram/telegram-progress-reporter.ts` `STAGE_LABELS`:

```ts
verifying:  'Double-checking...',
finalizing: 'Writing response...',
```

`to_response()` in `invoke.py` is unchanged (still returns `final_response`), but
we can optionally include `verification` in `tool_results`/a new field for
debugging — low priority.

---

## Risks & mitigations

- **Latency.** Deterministic finalize is microseconds; only the optional LLM
  verifier and the retry add a model round-trip. Keep verifier off by default;
  cap retries at 1.
- **Infinite loops.** `finalize_attempts` + `MAX_FINALIZE_RETRIES` hard cap;
  on exhaustion, ship an honest reply, never loop.
- **Double formatting.** Node emits GFM only; the TS MarkdownV2 layer is the sole
  Telegram renderer. Do not emit `<tg-*>`/HTML.
- **False-positive verification** (flagging a correct answer). Keep `hard`
  severity narrow: only fire on concrete envelope failures / blocks / declines /
  unambiguous claim-vs-action mismatch. Everything else is `soft` (ship).
- **Empty-string regression.** The empty-reply guard actually *fixes* an existing
  bug where `response: ""` can reach the client.

---

## Implementation phases

1. **Deterministic finalize** — new node + state fields + rewire agent route.
   Formatting pass, verification verdict, route_after_finalize, empty-reply
   guard. No LLM, no retry yet (always route `end`). Ship value immediately.
2. **Verify-and-correct loop** — enable the `hard` → agent bounce with the
   bounded counter and the honest-disclosure fallback.
3. **Receipts appendix** — derive the audit line from envelopes; flag-gated.
4. **Optional LLM verifier / suggestions** — behind config flags, after the
   deterministic core is proven.

## Files to touch

- `agents/agent_api/app/graph/nodes/finalize.py` — **new**; `create_finalize_node`
  factory + `format_final_reply` + `verify_completion` helpers (mirror the
  factory pattern of `nodes/summarize.py`).
- `agents/agent_api/app/graph/state.py` — add `draft_response`,
  `verification`, `finalize_attempts`.
- `agents/agent_api/app/graph/builder.py` — register the `finalize` NodeSpec;
  add `finalize` to the `agent` route_map; add `route_after_finalize`.
- `agents/agent_api/app/graph/nodes/orchestrator.py` — answer branch writes
  `draft_response` and routes to `finalize` instead of `end`.
- `agents/agent_api/app/graph/prompts/` — (phase 4) verifier prompt; optionally
  a correction-note template for the retry loop.
- `src/services/telegram/telegram-progress-reporter.ts` — add `verifying` /
  `finalizing` stage labels.

## Verification (how to test this change)

- **Unit tests** (`agents/.../tests`, follow existing node test patterns) over
  `verify_completion` and `format_final_reply` with synthetic `JarvisState`:
  - clean success (read query) → `ok`, draft unchanged, route `end`.
  - draft claims "created task" + `create_task` envelope `success=false` →
    severity `hard`, route `agent` (phase 2) or honest rewrite (phase 1 fallback).
  - `mutation_blocked=true` → blocked surfaced, no false success.
  - `confirm_decision="decline"` but draft claims action → `hard`.
  - empty draft → fallback string, never `""`.
  - whole-reply code fence / trailing filler → stripped.
- **Graph/integration test** through `run_jarvis` asserting the answer path now
  visits `finalize` and the retry loop terminates within `MAX_FINALIZE_RETRIES`.
- **End-to-end smoke**: `uvicorn agents.api:app ...`, POST `/invoke` with a
  request that triggers a failing mutation, confirm the reply discloses the
  failure rather than claiming success. Then run `npm test -- --runInBand`,
  `npm run test:integration -- --runInBand`, `npm run build`, `npm run lint`.

## Open decisions

- **D1 — LLM verifier in v1?** Recommendation: no; deterministic first.
- **D2 — Retry budget.** `MAX_FINALIZE_RETRIES = 1` proposed. More risks latency.
- **D3 — Next-step suggestions.** Conflicts with the current "don't offer further
  help" prompt rule. Keep off unless the prompt rule is revisited.
- **D4 — Receipts default.** On for multi-mutation batches, off for read-only
  queries? Or fully flag-gated off in v1?
