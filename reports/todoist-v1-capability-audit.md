# Todoist API v1 capability audit

**Date:** 2026-07-13  
**Official reference:** <https://developer.todoist.com/api/v1/>  
**Repository scope:** `agents/agent_api/app/tools/todoist/`

## Executive summary

The current Todoist integration exposes **14 agent tools**. Task support is a
reasonable CRUD foundation, but the project-organization layer is incomplete:

- Projects can only be listed and created.
- **Sections have no read or write tool at all.**
- A task schema accepts `section_id`, but the agent has no way to discover a
  project's sections or resolve a section name to an ID.

This means an agent cannot reliably execute a request such as “put this task in
the *In Progress* section of the *Roadmap* project.” It can resolve the project
ID through `get_projects`, but it cannot resolve `In Progress` to a `section_id`.

Todoist v1 comprises two complementary API surfaces:

1. **REST API** — use for ordinary resource reads and single-resource CRUD.
2. **Sync API (`POST /api/v1/sync`)** — use for incremental synchronization,
   batched changes, optimistic temporary IDs, and operations such as project or
   section movement/reordering that do not have REST equivalents.

## Current implementation inventory

The implementation has three required registration layers:

- `schemas.py`: the JSON function schemas visible to the model.
- `tools.py`: handler registry and LangChain wrappers.
- `client.py`: Todoist HTTP client methods.

`tests/agents/test_todoist_v1_tools.py` asserts that all three tool-name sets
are identical. New tools must therefore be added to every layer.

### Exposed tools (14)

| Domain | Tools | Notes |
|---|---|---|
| Tasks | `add_todoist_task`, `get_todoist_task`, `get_tasks`, `get_tasks_by_filter`, `update_todoist_task`, `complete_task`, `uncomplete_task`, `delete_todoist_task`, `get_completed_todoist_tasks_by_completion_date` | Strongest current area. Create accepts `project_id`, `section_id`, parent, labels, assignment, due dates, duration, and deadline. |
| Comments | `get_comments`, `add_comment` | Supports task or project comments; no update/delete or attachment support. |
| Labels | `get_labels` | Lists only; `search` is implemented as a local substring filter after listing. |
| Projects | `get_projects`, `create_project` | `search` is likewise local filtering, not Todoist's dedicated search endpoint. |

### Existing project behavior

`get_projects` calls `GET /projects` with cursor/limit and optionally filters
the returned page locally by name. `create_project` calls `POST /projects` and
supports `name`, `description`, `parent_id`, `color`, `is_favorite`, and
`view_style`.

It does **not** expose project detail, update, delete, archive/unarchive,
collaborators, permissions, archived-project listing, joining a workspace
project, or project movement/reordering.

### Existing section behavior

There is no `/sections` client call, schema, wrapper, or test. Section IDs only
appear as opaque optional task parameters (`add_todoist_task`, `get_tasks`, and
the completed-task query). The agent cannot acquire a valid ID itself.

## Complete agent-tool catalog available from Todoist v1

This is a capability catalog, not a recommendation to expose every endpoint to
the model. The preferred implementation is a small, coherent tool vocabulary
with safe compound reads and explicit confirmation for consequential writes.

### Tasks

| Capability | REST support | Recommended agent tool |
|---|---|---|
| List active tasks | `GET /tasks` | `get_tasks` (already present) |
| Get one task | `GET /tasks/{task_id}` | `get_todoist_task` (already present) |
| Create structured task | `POST /tasks` | `add_todoist_task` (already present) |
| Natural-language capture | `POST /tasks/quick` | `quick_add_task` |
| Update task | `POST /tasks/{task_id}` | `update_todoist_task` (already present) |
| Move task | `POST /tasks/{task_id}/move` or Sync | `move_task` |
| Complete / close | `POST /tasks/{task_id}/close` | `complete_task` (already present) |
| Reopen | `POST /tasks/{task_id}/reopen` | `uncomplete_task` (already present) |
| Delete | `DELETE /tasks/{task_id}` | `delete_todoist_task` (already present; destructive) |
| Filter-query list | `GET /tasks/filter` | `get_tasks_by_filter` (already present) |
| Completed history | completion/due-date endpoints and stats | `get_completed_tasks_by_due_date`, `get_completion_stats` |
| Reorder / day order / recurring completion | Sync | `reorder_tasks`, `update_day_orders`, `complete_recurring_task` |

### Projects

| Capability | REST support | Recommended agent tool |
|---|---|---|
| List active projects | `GET /projects` | `get_projects` (already present) |
| Search projects | `GET /projects/search` | `search_projects` |
| List archived projects | `GET /projects/archived` | `get_archived_projects` |
| Get one project | `GET /projects/{project_id}` | `get_project` |
| Create | `POST /projects` | `create_project` (already present) |
| Update | `POST /projects/{project_id}` | `update_project` |
| Archive / unarchive | project archive endpoints | `archive_project`, `unarchive_project` |
| Delete | `DELETE /projects/{project_id}` | `delete_project` (destructive) |
| Join workspace project | `POST /projects/{project_id}/join` | `join_project` |
| Collaborators | `GET /projects/{project_id}/collaborators` | `get_project_collaborators` |
| Permissions | `GET /projects/permissions` | `get_project_permissions` |
| Move, reorder, workspace transfer, leave, role change | Sync only | `move_project`, `reorder_projects`, `move_project_to_workspace`, `leave_project`, `change_project_role` |

### Sections

| Capability | REST support | Recommended agent tool |
|---|---|---|
| List active sections, optionally per project | `GET /sections` | `get_sections` |
| Search sections by name | `GET /sections/search` | `search_sections` |
| Get one section | `GET /sections/{section_id}` | `get_section` |
| Create | `POST /sections` | `create_section` |
| Update name, description, order, collapsed state | `POST /sections/{section_id}` | `update_section` |
| Archive / unarchive | section archive endpoints | `archive_section`, `unarchive_section` |
| Delete section and its tasks | `DELETE /sections/{section_id}` | `delete_section` (destructive) |
| Move / bulk reorder | Sync only | `move_section`, `reorder_sections` |

Section creation accepts `project_id`, `name`, optional `order`, and optional
description. The section list endpoint accepts `project_id`, `cursor`, and
`limit`, which is exactly the missing discovery path.

### Collaboration and organization

| Resource | REST capabilities | Suggested tool family |
|---|---|---|
| Comments | List/read/create/update/delete; create supports attachments and users to notify | `get_comment`, `get_comments`, `add_comment`, `update_comment`, `delete_comment`, `upload_file` |
| Labels | List/search/read/create/update/delete; shared-label list/rename/remove | `get_labels`, `search_labels`, `create_label`, `update_label`, `delete_label`, `rename_shared_label`, `remove_shared_label` |
| Filters | Sync CRUD and bulk ordering for personal/workspace filters | `get_filters`, `create_filter`, `update_filter`, `delete_filter` |
| Reminders | CRUD time reminders and location reminders | `get_reminders`, `create_reminder`, `update_reminder`, `delete_reminder`; location equivalents |
| Folders | Workspace-folder CRUD | `get_folders`, `get_folder`, `create_folder`, `update_folder`, `delete_folder` |
| Templates | Export project; import/create project from file or template ID | `export_project_template`, `import_project_template`, `create_project_from_template` |
| Uploads | Upload/delete file resources | `upload_file`, `delete_upload` |
| Workspaces | CRUD, members, invitations, plan details, projects | Admin-gated `workspace_*` tools |

### Read-only intelligence and automation

- `get_user_info`
- `get_productivity_stats`
- `get_activity_log`
- `get_backups` / `download_backup` (scope-gated)
- `get_id_mappings`
- webhook event ingestion and incremental Sync state updates

### Integration/admin surface (normally exclude from a general agent)

- OAuth authorization, token refresh/revocation, dynamic client registration.
- App/webhook configuration.
- Account email enable/disable operations.
- Billing subscription actions.
- Workspace membership, invitation, and deletion operations.

These tools can cause security, billing, or organization-wide effects. If they
are exposed at all, require narrow OAuth scopes, an explicit confirmation path,
and audit logging.

## Recommended project-and-section design

### Phase 1: unblock normal project planning

Implement these REST-backed tools first:

1. `get_project(project_id)`
2. `search_projects(query, cursor?, limit?)`
3. `update_project(project_id, ...)`
4. `archive_project(project_id)` / `unarchive_project(project_id)`
5. `get_project_collaborators(project_id)`
6. `get_sections(project_id?, cursor?, limit?)`
7. `search_sections(query, project_id?, cursor?, limit?)`
8. `get_section(section_id)`
9. `create_section(project_id, name, description?, order?)`
10. `update_section(section_id, name?, description?, order?, is_collapsed?)`
11. `archive_section(section_id)` / `unarchive_section(section_id)`

Add `delete_project` and `delete_section` only with the existing mutation gate
and a high-risk confirmation policy. Deleting a section deletes its tasks.

### Phase 2: make structure inspection one safe call

Add a composite read tool:

```text
get_project_structure(project_id, include_tasks=true, include_archived=false)
  -> project
  -> sections[]       (all cursor pages)
  -> tasks[]          (all cursor pages or a bounded summary)
  -> next_cursor / truncation metadata where applicable
```

This should become the standard precursor for project-planning prompts. It
minimizes model round trips and makes section IDs visible before task creation.

### Phase 3: use Sync for plan reshaping

Implement Sync-backed operations for:

- project/section/task moves and reordering;
- atomic setup of project, sections, tasks, and subtasks;
- bulk day-order changes;
- recurring-task completion controls;
- incremental cache updates after changes.

Sync accepts up to 100 commands in one request. Temporary IDs let a newly
created project be referenced by sections and tasks within that same batch.

## Safety, grounding, and API constraints

### Tool policy changes required

- Add every writer to `MUTATING_TOOL_NAMES`.
- Mark delete operations, workspace membership changes, invitations, and billing
  operations as explicit-confirmation actions.
- Extend entity grounding beyond tasks. The current grounding infrastructure
  validates task IDs, but not project or section IDs.
- For any operation using a project or section ID, require a same-conversation
  entity reference returned by a prior read, not a model-invented ID.
- Add a section equivalent to the current project-resolution prompt guidance:
  resolve project -> list/search sections -> use returned section ID -> create
  or move task.

### Pagination

Most list endpoints return `results` and an opaque `next_cursor`. The tool
schema should accept `cursor` and a bounded `limit`; callers must pass a cursor
verbatim and preserve the original filters between pages. General endpoint
limits are commonly 200 items per page, though individual endpoints can differ.

### Request limits

- Standard request processing timeout: 15 seconds.
- Upload processing timeout: 5 minutes.
- Maximum POST body: 1 MiB (attachment allowance depends on plan).
- Sync: 100 commands per request.
- Per user, per 15 minutes: 1,000 partial Sync requests or 100 full Sync
  requests.

Prefer one initial full Sync followed by incremental Sync using the returned
token. Use REST for focused reads and simple individual writes.

### OAuth scopes

Relevant official scopes include `task:add`, `data:read`, `data:read_write`,
`data:delete`, `project:delete`, and `backups:read`. Design scopes around the
actual enabled tools; do not grant delete or backup privileges merely because
the API can support them.

## Test plan

At a minimum, add tests for:

1. Schema, registry, wrapper, and client consistency for every new tool.
2. `GET /sections?project_id=...` pagination and section search.
3. Project -> section discovery -> task creation end-to-end flow.
4. Create/update payload shaping, including explicit null-clears where Todoist
   supports them.
5. Mutation gate and confirmation behavior for archive/delete/move operations.
6. Entity grounding rejection of unobserved project and section IDs.
7. Sync request construction, command UUIDs, temporary-ID mapping, and partial
   failure handling.
8. Cursor preservation and no fabricated cursor values.

## Priority order

1. **Sections:** list/search/read/create/update/archive/unarchive.
2. **Project detail and updates:** get/search/update/archive/unarchive.
3. **Composite structure reader:** project + sections + tasks.
4. **Safe destructive support:** delete project/section/comment/label.
5. **Task quality:** Quick Add, move, due-date history/stats, recurring controls.
6. **Collaboration:** project collaborators, comment update/delete/attachments.
7. **Sync operations:** project/section/task movement and ordering.
8. **Broader organization:** labels, filters, reminders, folders, templates,
   workspaces, and activity.

## References

- Todoist API v1: <https://developer.todoist.com/api/v1/>
- Implementation schemas: `agents/agent_api/app/tools/todoist/schemas.py`
- Tool registration/wrappers: `agents/agent_api/app/tools/todoist/tools.py`
- REST client: `agents/agent_api/app/tools/todoist/client.py`
- Existing v1 tests: `tests/agents/test_todoist_v1_tools.py`
