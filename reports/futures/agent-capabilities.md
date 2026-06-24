# Agent Capability Enhancements

> **Status overview (2026-06-24):** Tool Output Summarization ✅, HITL/Clarification nodes ✅, Tool Retry (client-level) ✅. Most other items not started.

## HITL "Chat Instead" Option ❌
**Status:** Not started. Confirm/Decline only; no third "Chat Instead" option.

For risky actions: user currently has to confirm/decline. Add a third option to "chat instead" — lets the user discuss the action before committing.

## Planner Node ❌
**Status:** Not started. No dedicated planner node in graph. Worker prompt drafted (`prompts/worker.py`) but not wired.

Add a planner node first to map out the tasks needed, then send the plan to orchestrator to spawn workers.

Example query: "Go through my tasks, check everything that does not have a time, that is also not a birthday. Tell me and I will ask you to make edits."

Consider a DAG structure: https://github.com/arunpshankar/Agentic-Workflow-Patterns/tree/main/src/patterns/dag_orchestration (possible in LangGraph).

## Conflict Detection Layer ❌
**Status:** Not started. No conflict check when tasks are added. No duplicate detection.

Every time a task is added, run a conflict check that notifies the user and asks for next steps (keep both? keep one?).

## Replace HITL Upgrade Routing ✅
**Status:** Done. HITL node exists and routes `ask_user` tool calls via `interrupt()`. Proper routing in `hitl.py`.

Replace "hitl_upgrade" with a proper FINAL node so that "?" in model final answer doesn't just get routed to hitl_upgrade.

## Clarification Node ✅
**Status:** Done. Clarification handled via `ask_user` tool → HITL node → interrupt payload with `type: "clarify"`. Users receive clarification via `pending-clarification.store.ts`.

User needs a way to tell if they are being asked a clarification question (so they know if the current chat is still active or a new query can be started). Solution: a dedicated clarification node, or a tag indicating clarification is required.

## Tool Retry Node 🟡
**Status:** Partial. Retry logic exists at Todoist client level (exponential backoff, error taxonomy). Orchestrator handles model-level retries. **Gap:** No dedicated graph-level retry node for generic tool errors.

Note: Todoist API retry/error-taxonomy is ✅ done at the client level (4.2). This item is about a graph-level retry node that catches tool errors generically.

Tool errors should be retried using a dedicated tool retry node — don't leave retry logic up to the orchestrator.

## Agent Memory ❌
**Status:** Not started. No persistent agent memory or write-to-memory flow.

If something is deemed worth remembering, write it to memory (parallel call alongside the response).

## Schedule Conflict Awareness ❌
**Status:** Not started. No conflict detection when tasks are scheduled. No event lookup.

When adding a task (e.g. "add meet zac on friday night"), pull events from +/- 1 hour and report conflicts back to the user.

## Tool Output Summarization ✅
**Status:** Done. Summarize node implemented and wired in `nodes/summarize.py`. Threshold: `SUMMARIZE_THRESHOLD=20` tasks triggers summarization. Preserves task IDs, names, due dates, priority, project, labels.

`get_tasks` can return 200 tasks as raw JSON, bloating context. Add a summarize node before the next agent turn if the result exceeds a certain context length.
