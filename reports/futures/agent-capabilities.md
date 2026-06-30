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


## Tool Retry Node 🟡
**Status:** Partial. Retry logic exists at Todoist client level (exponential backoff, error taxonomy). Orchestrator handles model-level retries. **Gap:** No dedicated graph-level retry node for generic tool errors.

Note: Todoist API retry/error-taxonomy is ✅ done at the client level (4.2). This item is about a graph-level retry node that catches tool errors generically.

Tool errors should be retried using a dedicated tool retry node — don't leave retry logic up to the orchestrator.

## Agent Memory ❌
**Status:** Not started. No persistent agent memory or write-to-memory flow.

If something is deemed worth remembering, write it to memory (parallel call alongside the response).
