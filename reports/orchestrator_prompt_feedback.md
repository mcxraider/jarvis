# Orchestrator Prompt Assessment & Recommendations

Last updated: 2026-06-23

## Context

Assessment of whether the current orchestrator prompt (`agents/agent_api/app/graph/prompts/orchestrator.py`) is production-ready, and what gaps exist between the prompt and the actual LangGraph infrastructure.

---

## Verdict: Solid foundation, but has contradictions and gaps that would confuse the LLM in production

The prompt structure is good — clear decision tree, well-calibrated clarification policy, explicit failure handling. But it has dead references to features that don't exist, misses critical context about features that DO exist, and lacks tool-specific guidance that would prevent common LLM mistakes.

---

## Problems to Fix

### 1. Dead Worker/Dispatch References (confuses the model)

The preamble says *"You decompose complex requests and dispatch independent subtasks to workers"* — but DISPATCH doesn't exist. The failure section mentions "worker error" and limits section mentions "re-query a single worker". The model thinks it has capabilities it doesn't.

### 2. Reasoning Effort Section is Unactionable

"Think High / Think Max / Non-think" — DeepSeek Reasoner's API doesn't expose a reasoning-effort toggle. `temperature=0` is hardcoded. This section wastes ~50 tokens per request and the model cannot act on it.

### 3. Model Has Zero Awareness of the Confirm Gate

When the model calls `delete_todoist_task`, the system silently intercepts it for user approval. The model expects an immediate tool result but instead the graph pauses. After decline, it sees `"Action declined"` with no prompt context for why. Same for bulk mutations (5+). This causes:
- Model may retry declined actions
- Model doesn't know to warn users about confirmation prompts
- Model can't explain deferred safe calls

### 4. No Tool-Specific Guidance

The model frequently makes these mistakes without guidance:
- **Priority is inverted**: `priority: 4` = urgent, `1` = normal (counter-intuitive)
- **Date format confusion**: `due_string` (natural lang) vs `due_date` (YYYY-MM-DD) vs `due_datetime` (RFC3339)
- **Invalid filter syntax** for `get_tasks_by_filter` (model guesses SQL-like filters)
- **Fabricating task IDs** instead of fetching them first
- **Retrying `add_todoist_task`** on timeout → creates duplicates

### 5. No Timezone in Runtime Context

The prompt injects `Current date: 2026-06-23` but no timezone. For a calendar assistant constructing `due_datetime` (RFC3339 requires offset), the model guesses or uses UTC.

### 6. Missing Discovery Tools

The model knows about `project_id`, `section_id`, `labels` parameters but has NO tools to look up valid values. It either hallucates IDs or asks the user for information they shouldn't need to provide.

### 7. Grammar Issues

"the Jerry's" appears twice — should be "Jerry's".

---

## Recommended Changes

### Quick Wins (prompt-only, implement now)

1. **Rewrite preamble** — Remove worker/dispatch language. Replace with: "You handle requests by calling tools directly, chaining multiple calls when needed."

2. **Delete "Reasoning effort" section** entirely.

3. **Add Confirm Gate section:**
   ```
   ## Confirm gate (system-managed)
   Deletions and bulk mutations (5+ in one turn) are intercepted for user
   approval before execution. You'll receive the result after the user decides.
   If declined, acknowledge gracefully — do not retry unless the user explicitly
   asks again.
   ```

4. **Add Todoist tool tips section:**
   ```
   ## Todoist tool tips
   - Prefer due_string for dates ("tomorrow 3pm", "next monday") — Todoist parses natural language.
   - Priority is inverted: 4 = urgent, 3 = high, 2 = medium, 1 = normal (default).
   - Filter examples: "today", "overdue", "due before: next week", "p1", "#Work", "@label".
   - Never fabricate task IDs — always fetch first with get_tasks or get_tasks_by_filter.
   - Do not retry add_todoist_task on timeout — verify with get_tasks_by_filter instead.
   - Multiple safe tool calls in one turn execute in parallel — use this for efficiency.
   ```

5. **Fix grammar**: "the Jerry's" → "Jerry's"

6. **Remove "worker" references** from failure section and limits section.

### Medium Effort (small infra + prompt)

7. **Inject user timezone** into runtime context (e.g., `User timezone: Asia/Taipei`). Source from env var or Todoist user profile API.

8. **Add `get_projects` and `get_labels` tools** — two simple read-only REST wrappers so the model can discover valid project/label values.

9. **Fix max-turns behavior** — Either change infra to match prompt (interrupt at turn 7 with partial answer) or update prompt to say "stop with best partial answer" instead of claiming it will ASK_USER.

### Larger Efforts (future features)

10. **Cross-session memory** — User preferences, frequent project IDs, names. Currently every thread is stateless.

11. **Tool output summarization** — `get_tasks` can return 200 tasks as raw JSON, bloating context. Summarize before next agent turn.

12. **DISPATCH/workers** — Only needed when tool surface grows (Gmail, Calendar, Notion). Leave prompt aspirational language removed until then.

---

## Implementation Plan (Quick Wins)

**File**: `agents/agent_api/app/graph/prompts/orchestrator.py`

Changes to `ORCHESTRATOR_PROMPT`:
1. Replace first paragraph (remove dispatch/worker language)
2. Delete `## Reasoning effort` section (lines 35-36)
3. Fix "the Jerry's" → "Jerry's" (2 occurrences)
4. Change "Tool or worker error" → "Tool error" in failure section
5. Remove "One follow-up call to re-query a single worker also counts as one" from limits
6. Add new `## Confirm gate` section after clarification policy
7. Add new `## Todoist tool tips` section before final answer formatting

**File**: `agents/agent_api/app/graph/prompts/context.py`
- Add timezone to `get_orchestrator_prompt()` runtime context block

---

## Verification

- Run existing tests: `cd agents && python -m pytest tests/ -v`
- Manual test via `/invoke` API: send "delete my task X" and verify model doesn't retry after decline
- Manual test: send "add task for tomorrow at 3pm" and verify model uses `due_string`
- Check token count of new prompt (should be <800 tokens, currently ~650)
