# Staged Todoist Tool Registry Replacement

## Summary

Replace the current Python Todoist tool surface with exactly the 23 tools listed in `agents/agent_api/app/tools/todoist/todoist_tools_complete.md`, using the matching local TypeScript files in `/Users/Jerry_YANG_from.TP/Desktop/todoist-mcp/tools/` as the behavior source of truth. Keep the current stdlib HTTP client; do not add a Todoist SDK dependency.

Old model-facing names like `add_todoist_task`, `get_tasks_by_filter`, and `bulk_add_todoist_tasks` will be removed from the exposed registry.

## Stage 1: Registry, Schemas, And Prompt Contract

- Replace the Todoist schema catalogue with the 23 markdown tools, preserving hyphenated names exactly.
- Keep `ask_user` as the always-available control pseudo-tool.
- Rebuild `get_todoist_tool_specs()` so every markdown schema has a matching Python handler placeholder or implemented handler.
- Update mutating-tool names to the new catalogue.
- Update orchestrator Todoist guidance by replacing the current `## Todoist tool tips` section with:

```md
## Todoist tool tips
- Use `add-tasks` to create one or many tasks. Do not loop individual creates when one `add-tasks` call can create the requested batch.
- Use `find-tasks` for general task lookup by text, project, section, parent, labels, saved filter, or raw Todoist filter.
- Use `find-tasks-by-date` for date-based planning such as today, tomorrow, overdue, this week, or a date range.
- Never fabricate task IDs. Before `update-tasks`, `complete-tasks`, `uncomplete-tasks`, `reschedule-tasks`, `manage-assignments`, or `delete-object` with `type: "task"`, first retrieve matching tasks with `find-tasks` or `find-tasks-by-date` unless the user already supplied exact task IDs.
- Use `reschedule-tasks` when moving existing task due dates. Do not use `update-tasks` for rescheduling, because replacing due strings can damage recurring schedules.
- Use `update-tasks` for changing task content, description, labels, priority, project, section, parent, deadline, assignment, duration, or uncompletable status.
- For task creation or updates, priority uses `p1`, `p2`, `p3`, `p4`; `p1` is highest and `p4` is default/lowest. Do not send integer priorities.
- For natural language dates on create/update, use `dueString` such as "tomorrow 3pm" or "next monday". For rescheduling, use `reschedule-tasks` with `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`.
- If a create request times out or returns an uncertain failure, verify with `find-tasks` before retrying so you do not create duplicates.
- Multiple safe read calls in one turn may execute in parallel. For dependent mutations, fetch first, observe results, then mutate.
- Pagination: results may include a cursor/next cursor field. If no next cursor is returned, all results are returned. Only pass cursor values received verbatim from a prior response.
```

- Change runtime context from `Available tools: Todoist task tools only.` to `Available tools: Todoist tools only.`

Stage 1 checks:

- Add/update tests asserting the exposed tool names are exactly `ask_user` plus the 23 markdown tools.
- Add tests that old names are not exposed.
- Run:
  - `python3 -m pytest tests/agents/test_jarvis.py -k "ToolSelection or todoist"`
  - `python3 -m pytest tests/agents/test_risk_classifier.py tests/agents/test_metadata.py`
  - `npm run build`
  - `git diff --check`

## Stage 2: Core Task Tools

- Implement close Python ports for:
  - `add-tasks`
  - `find-tasks`
  - `find-tasks-by-date`
  - `find-completed-tasks`
  - `update-tasks`
  - `complete-tasks`
  - `uncomplete-tasks`
  - `reschedule-tasks`
- Port required helpers from TypeScript behavior: priority conversion, duration parsing, label filters, responsible-user filtering, date-window handling, and recurrence-safe rescheduling.
- Preserve current tracing, retry, redaction, and Todoist error classification through the existing Python client.
- Update confirmation metadata so task mutations render clearly with task context where available.

Stage 2 checks:

- Add mocked client tests for URL/method/payload shape.
- Port representative TS tests for task creation, lookup, date lookup, completion lookup, update, complete/uncomplete, and reschedule.
- Run:
  - `python3 -m pytest tests/agents/test_jarvis.py`
  - `python3 -m pytest tests/agents/test_confirm_node.py tests/agents/test_prepare_confirm_node.py`
  - `python3 -m pytest tests/agents/test_edges_route_after_agent.py tests/agents/test_risk_classifier.py`
  - `npm run build`
  - `git diff --check`

## Stage 3: Supporting Todoist Objects And Search

- Implement close Python ports for:
  - `add-comments`
  - `find-comments`
  - `add-filters`
  - `find-filters`
  - `find-labels`
  - `add-reminders`
  - `find-reminders`
  - `delete-object`
  - `fetch`
  - `fetch-object`
  - `search`
- Use Todoist REST/Sync endpoints through the existing stdlib client.
- Ensure `delete-object` handles each markdown-supported type and remains always risky/irreversible.
- Keep object fetch/search outputs compact enough for the agent loop.

Stage 3 checks:

- Add schema and handler dispatch tests for every supporting-object tool.
- Add mocked HTTP tests for comments, filters, labels, reminders, fetch, search, and delete cases.
- Run:
  - `python3 -m pytest tests/agents/test_dispatcher.py tests/agents/test_metadata.py`
  - `python3 -m pytest tests/agents/test_jarvis.py -k "todoist or tool"`
  - `npm run build`
  - `git diff --check`

## Stage 4: Overview, Activity, Health, And Assignments

- Implement close Python ports for:
  - `find-activity`
  - `get-overview`
  - `get-project-activity-stats`
  - `get-project-health`
  - `manage-assignments`
- Port enough shared helper logic to match TypeScript behavior for project summaries, completion stats, health calculations, assignment validation, dry-run handling, and rollback/failure reporting.
- Keep large outputs summarized, with structured content available for the agent to reason over.

Stage 4 checks:

- Add tests for overview/project health output shape, activity filters, assignment dry runs, assignment failures, and partial success behavior.
- Run:
  - `python3 -m pytest tests/agents`
  - `npm run build`
  - `npm test -- --runInBand` if TypeScript-side changes are made
  - `git diff --check`

## Stage 5: Compatibility Cleanup And Full Validation

- Remove or update old prompt/test references to:
  - `add_todoist_task`
  - `bulk_add_todoist_tasks`
  - `get_tasks`
  - `get_tasks_by_filter`
  - `update_todoist_task`
  - `complete_task`
  - `delete_todoist_task`
  - `get_completed_todoist_tasks_by_completion_date`
- Update fake Todoist clients and contract tests to use the new handler names.
- Verify the graph still routes read, low-risk mutation, risky deletion, ask-user, and confirmation flows correctly.
- Keep `get_todoist_tools()` only as a compatibility function returning the new schema list, not the old tools.

Final checks:

- `python3 -m pytest tests/agents`
- `npm run build`
- `npm test`
- `git diff --check`
- `git status -sb`

## Assumptions

- The markdown file is the public contract; extra TypeScript MCP tools not listed there are out of scope.
- The TypeScript implementations are the behavior reference for tools that exist in both places.
- The Python implementation keeps the current stdlib HTTP style and does not add a Todoist SDK.
- Old tool names are removed from model exposure rather than kept as aliases.
- `ask_user` remains unchanged and always available.
