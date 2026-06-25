# Schedule Conflict Detection — Post-Add Enrichment

## Context

When you tell the bot "add meeting at 3pm," it creates the task without knowing there's already a "Team standup" at 2:30–3:30pm. This is a double-booking. The goal is to detect and surface these conflicts without adding latency or requiring a separate fetch.

## Core Idea: Tool-Level Post-Add Enrichment

Instead of fetching before the add (latency) or after as a separate step (requires the LLM to "remember" to do it), we bake conflict detection **inside the add-task tool's return path**:

```
User: "add meeting at 3pm"
  → tool creates the task (Todoist resolves the time)
  → tool sees the result has due.datetime (specific time)
  → tool immediately queries same-day tasks (single API call, ~80ms)
  → tool computes overlaps
  → tool returns: { task: {...}, conflicts: [{...}] }
Agent sees conflicts in the tool result → informs user
```

**Why this is architecturally elegant:**
- The tool "enriches its own response" — no new graph nodes, no new tools, no extra LLM turns
- The detection is co-located with creation (cannot be forgotten or skipped by the LLM)
- Advisory only: the task is always created first; conflicts are informational
- The agent prompt tells it what to do when conflicts appear (surface them, offer to adjust)
- Works for both todoist-mcp (MCP server) and jarvis-mcp (Python agent) identically

## Implementation

### 1. New utility: `conflict-detection.ts` (todoist-mcp)

**File:** `Desktop/todoist-mcp/utils/conflict-detection.ts`

Pure functions, fully unit-testable:
- `detectConflicts(newTask, existingTasks, windowMinutes=30)` → `ConflictResult[]`
- Two slots overlap when `start1 < end2 && start2 < end1`
- Proximity: expand each slot by `windowMinutes` before checking overlap
- Tasks without `due.datetime` are skipped entirely (date-only tasks like "buy groceries" never conflict)
- Duration-less tasks treated as 30-minute blocks (sensible default for meetings)

Two conflict tiers:
- **overlap** — actual time collision
- **proximity** — within 30 minutes but not overlapping (back-to-back warning)

### 2. Expose `dueDateTime` in task output

**Files:** `Desktop/todoist-mcp/tool-helpers.ts` + `Desktop/todoist-mcp/utils/output-schemas.ts`

Add `dueDateTime: task.due?.datetime` to `mapTask()` and the corresponding Zod schema. Currently only `dueDate` (date-only) is exposed — the exact time is lost, which means conflict info wouldn't be useful without this.

### 3. Wire into `add-tasks.ts`

**File:** `Desktop/todoist-mcp/tools/add-tasks.ts`

After the task creation loop finishes (line ~148):
1. Filter created tasks to those with `due.datetime`
2. If any exist: query same-day tasks via `getTasksByFilter` (one call per unique date)
3. Run `detectConflicts()` excluding the newly created tasks from the existing set
4. Append `conflicts` + `hasConflicts` to `structuredContent`
5. Append warning lines to `textContent`

Add `skipConflictCheck` boolean param to TaskSchema — defaults to false, allows batch callers to opt out.

Wrapped in try-catch: if the fetch fails, conflicts are silently `[]` — never break the add.

### 4. Python mirror in jarvis-mcp

**Files:**
- `Desktop/jarvis-mcp/agents/agent_api/app/tools/todoist/client.py` — add `_detect_conflicts()` method, called after `add_todoist_task` succeeds
- `Desktop/jarvis-mcp/agents/agent_api/app/tools/todoist/schemas.py` — add `skip_conflict_check` param
- `Desktop/jarvis-mcp/agents/agent_api/app/tools/todoist/tools.py` — pass the param through

Same logic: POST create → check `due.datetime` → query same-day → find overlaps → attach `_conflicts` to result.

### 5. Prompt update (behavioral change)

**File:** `Desktop/jarvis-mcp/agents/agent_api/app/graph/prompts/orchestrator.py`

Add to `## Todoist tool tips`:
```
- Schedule conflicts: when add_todoist_task returns a `_conflicts` field, proactively tell Jerry which tasks overlap or are back-to-back. Offer to reschedule if the conflict is an overlap, or just note it for proximity warnings.
```

This is the only "behavioral" change needed — the agent already reads tool results and synthesizes answers.

## What the user sees

```
User: "Add design review at 3pm for 1 hour"
Bot: "Added 'Design review' at 3:00pm (1h).
      ⚠️ Heads up: 'Sprint planning' runs 2:30–3:30pm — that overlaps by 30 minutes.
      Want me to move one of them?"
```

## Key Design Choices

| Decision | Choice | Why |
|----------|--------|-----|
| Where does detection live? | Inside the tool `execute()` | Cannot be forgotten by LLM; no graph changes needed |
| Blocking or advisory? | Advisory (add always succeeds) | User said "add first, inform after" |
| Fetch before or after add? | After (from the created task's resolved time) | Todoist resolves `dueString` server-side; we only know exact time post-creation |
| Extra latency | ~80ms (one filter query) | Invisible; user already waits ~300ms for create |
| Scope of check | Same-day tasks with times | Keeps it relevant; doesn't flag unrelated date-only tasks |
| Default proximity window | 30 minutes | Catches back-to-back without noise |

## Verification

1. **Unit tests** for `conflict-detection.ts`: overlap, proximity, no-conflict, point events, edge cases
2. **Integration test** for `add-tasks.ts`: mock Todoist API, verify conflicts appear in response
3. **Manual E2E**: Add a task that conflicts with an existing one via Telegram, confirm the bot surfaces it
4. **Edge case**: Add task with `dueString: "tomorrow"` (no time) — verify no conflict check runs

## Files to Modify

- `Desktop/todoist-mcp/utils/conflict-detection.ts` (new)
- `Desktop/todoist-mcp/utils/output-schemas.ts` (add `dueDateTime` + conflict schema)
- `Desktop/todoist-mcp/tool-helpers.ts` (add `dueDateTime` to `mapTask`)
- `Desktop/todoist-mcp/tools/add-tasks.ts` (wire conflict detection)
- `Desktop/jarvis-mcp/agents/agent_api/app/tools/todoist/client.py` (Python mirror)
- `Desktop/jarvis-mcp/agents/agent_api/app/tools/todoist/schemas.py` (add param)
- `Desktop/jarvis-mcp/agents/agent_api/app/tools/todoist/tools.py` (pass param)
- `Desktop/jarvis-mcp/agents/agent_api/app/graph/prompts/orchestrator.py` (prompt tweak)
