# Tool schema for Todoist

This catalogue is split by the Todoist API surface expected to back each tool.

## v1 API-backed tools

These tools map to Todoist REST v1 endpoints.

| Tool | v1 endpoint(s) |
| --- | --- |
| `find-tasks` | `GET /api/v1/tasks`, `GET /api/v1/tasks/filter` |
| `add-tasks` | `POST /api/v1/tasks` |
| `update-tasks` | `POST /api/v1/tasks/{task_id}` |
| `complete-tasks` | `POST /api/v1/tasks/{task_id}/close` |
| `uncomplete-tasks` | `POST /api/v1/tasks/{task_id}/reopen` |
| `delete-object` with `type: "task"` | `DELETE /api/v1/tasks/{task_id}` |
| `find-completed-tasks` | `GET /api/v1/tasks/completed/by_completion_date` |
| `find-comments` | `GET /api/v1/comments` |
| `add-comments` | `POST /api/v1/comments` |
| `find-labels` | `GET /api/v1/labels`, `GET /api/v1/labels/search` |
| `find-reminders` | `GET /api/v1/reminders` |
| `add-reminders` | `POST /api/v1/reminders` |
| `find-activity` | `GET /api/v1/activities` |

### `add-comments`

**Description:** Add multiple comments to tasks or projects. Each comment must specify either `taskId` or `projectId`.

```ts
add-comments({
  comments: Array<{
    projectId?: string; // Project ID string, or "inbox" for inbox tasks
    taskId?: string;    // Task ID
    content: string;    // Required, minLength: 1
  }>
})
```

---

### `add-reminders`

**Description:** Add reminders to tasks. Supports three types: `relative`, `absolute`, or `location`. Each reminder must specify a `taskId`.

```ts
add-reminders({
  reminders: Array<
    | {
        type: "relative";

        taskId: string; // Required, minLength: 1

        minuteOffset: number;
        // Required
        // Minimum: 0
        // Maximum: 9007199254740991
        // Examples:
        // 30 = 30 minutes before
        // 60 = 1 hour before
        // 1440 = 1 day before

        service?: "email" | "push"; // Defaults to push
        isUrgent?: boolean;
      }

    | {
        type: "absolute";

        taskId: string; // Required, minLength: 1

        due: {
          timezone?: string; // Example: "America/New_York"
          string?: string;   // Natural language, e.g. "tomorrow at 3pm"
          lang?: string;     // Example: "en"
          date?: string;     // YYYY-MM-DD
        };

        service?: "email" | "push"; // Defaults to push
        isUrgent?: boolean;
      }

    | {
        type: "location";

        taskId: string; // Required, minLength: 1

        name: string; // Required, minLength: 1
        locTrigger: "on_enter" | "on_leave";

        locLat: string;  // Latitude as string, e.g. "37.7749"
        locLong: string; // Longitude as string, e.g. "-122.4194"

        radius?: number;
        // Minimum: -9007199254740991
        // Maximum: 9007199254740991
      }
  >
})
```

---

### `add-tasks`

**Description:** Add one or more tasks to a project, section, or parent. Supports assignment to project collaborators.

```ts
add-tasks({
  tasks: Array<{
    content: string; // Required, minLength: 1
    // Task name/title. Should be concise and actionable.
    // Supports Markdown.

    responsibleUser?: string;
    // Can be "me", a user ID, name, or email address.
    // User must be a collaborator on the target project.

    duration?: string;
    // Examples:
    // "2h"
    // "90m"
    // "2h30m"
    // "1.5h"
    // Max 24h

    priority?: "p1" | "p2" | "p3" | "p4";
    // Integers like 1, 2, 3, 4 are not accepted.

    projectId?: string;
    // Project ID string, or "inbox" for inbox tasks.

    deadlineDate?: string;
    // ISO date: YYYY-MM-DD
    // Example: "2025-12-31"

    dueString?: string;
    // Natural language due date.

    description?: string;
    // Additional details or notes.
    // Supports Markdown.

    parentId?: string;  // Parent task ID for subtasks
    order?: number;     // Position among sibling tasks
    labels?: string[];  // Labels to attach

    isUncompletable?: boolean;
    // Organizational header. Cannot be completed.

    sectionId?: string;
  }>
})
```

---

### `complete-tasks`

**Description:** Complete one or more tasks by their IDs.

```ts
complete-tasks({
  ids: Array<string> // Required task IDs, each minLength: 1
})
```

---

### `find-activity`

**Description:** Retrieve recent activity logs to monitor and audit changes in Todoist. Shows events from all users by default. Date-based filtering is not supported by the Todoist API.

```ts
find-activity({
  limit?: number;
  // Default: 20
  // Minimum: 1
  // Maximum: 100

  initiatorId?: string;
  // Filter by the user ID who initiated the event.

  objectType?: "task" | "project" | "comment";

  eventType?:
    | "added"
    | "updated"
    | "deleted"
    | "completed"
    | "uncompleted"
    | "archived"
    | "unarchived"
    | "shared"
    | "left";

  projectId?: string;
  // Filter by parent project ID.

  taskId?: string;
  // Filter by parent task ID.

  objectId?: string;
  // Filter by specific object ID.

  cursor?: string;
  // Pagination cursor.
})
```

---

### `find-comments`

**Description:** Find comments by task, project, or get a specific comment by ID. Exactly one of `taskId`, `projectId`, or `commentId` must be provided.

```ts
find-comments({
  taskId?: string;
  projectId?: string;
  // Project ID string, or "inbox" for inbox tasks.

  commentId?: string;

  limit?: number;
  // Minimum: 1
  // Maximum: 10

  cursor?: string;
  // Pagination cursor.
})
```

---

### `find-completed-tasks`

**Description:** Get completed tasks. `since` and `until` are optional and default to a 7-day window when omitted. Includes all collaborators by default. Person-specific queries require `responsibleUser`.

```ts
find-completed-tasks({
  getBy?: "completion" | "due";
  // Default: "completion"
  // "completion" = by actual completion date
  // "due" = by due date

  responsibleUser?: string;
  // User ID, name, or email.
  // For personal summaries, should be current user.

  since?: string;
  // YYYY-MM-DD
  // Defaults to 6 days before until, or today if until omitted.

  until?: string;
  // YYYY-MM-DD
  // Defaults to 6 days after since, or today if since omitted.

  projectId?: string;
  // Project ID string, or "inbox" for inbox tasks.

  parentId?: string;
  sectionId?: string;
  workspaceId?: string;

  labels?: string[];

  labelsOperator?: "and" | "or";
  // Default: "or"

  limit?: number;
  // Default: 50
  // Minimum: 1
  // Maximum: 200

  cursor?: string;
})
```

---

### `find-labels`

**Description:** List personal labels and shared labels. Personal labels have full metadata. Shared labels are returned as names only. Supports pagination and name search.

```ts
find-labels({
  searchText?: string;
  // Partial and case-insensitive search.
  // Supports wildcards, e.g. "work*".
  // Use "\\*" for a literal asterisk.
  // If provided, all matching personal labels are fetched across pages.

  limit?: number;
  // Default: 50
  // Minimum: 1
  // Maximum: 200
  // Ignored when searchText is provided.

  cursor?: string;
  // Pagination cursor.
  // Ignored when searchText is provided.
})
```

---

### `find-reminders`

**Description:** Find reminders by task ID, or get a specific reminder by its ID. Returns all reminder types for a task.

```ts
find-reminders({
  taskId?: string;
  // Find all reminders for a specific task.

  reminderId?: string;
  // Get a specific time-based reminder.

  locationReminderId?: string;
  // Get a specific location reminder.
})
```

---

### `find-tasks`

**Description:** Find tasks by text search, project, section, parent container, responsible user, labels, raw Todoist filter string, or saved filter. At least one filter must be provided.

```ts
find-tasks({
  searchText?: string;
  // Text search in tasks.

  filter?: string;
  // Raw Todoist filter query.
  // Examples:
  // "today"
  // "p1"
  // "##Work"
  // "(today | overdue) & p1"
  // Cannot be used with projectId, sectionId, parentId, or filterIdOrName.

  filterIdOrName?: string;
  // Saved Todoist filter ID or name.
  // Cannot be used with filter, projectId, sectionId, or parentId.

  projectId?: string;
  // Project ID string, or "inbox" for inbox tasks.

  sectionId?: string;
  parentId?: string;

  responsibleUser?: string;
  // User ID, name, or email.

  responsibleUserFiltering?: "assigned" | "unassignedOrMe" | "all";
  // Default: "unassignedOrMe"
  // "assigned" = only tasks assigned to others
  // "unassignedOrMe" = unassigned tasks or tasks assigned to me
  // "all" = all tasks regardless of assignment

  labels?: string[];

  labelsOperator?: "and" | "or";
  // Default: "or"

  limit?: number;
  // Default: 10
  // Minimum: 1
  // Maximum: 100

  cursor?: string;
})
```

---

### `uncomplete-tasks`

**Description:** Uncomplete, or reopen, one or more completed tasks by their IDs.

```ts
uncomplete-tasks({
  ids: Array<string> // Required task IDs, each minLength: 1
})
```

---

### `update-tasks`

**Description:** Update existing tasks including content, dates, priorities, and assignments.

```ts
update-tasks({
  tasks: Array<{
    id: string; // Required, minLength: 1

    content?: string;
    // New task name/title.
    // Should be concise and actionable.
    // Supports Markdown.

    responsibleUser?: string;
    // Change task assignment.
    // Use "unassign" to remove assignment.
    // Can be "me", user ID, name, or email.
    // User must be a project collaborator.

    duration?: string;
    // Examples:
    // "2h"
    // "90m"
    // "2h30m"
    // "1.5h"
    // Max 24h

    priority?: "p1" | "p2" | "p3" | "p4";

    projectId?: string;
    // New project ID, or "inbox".

    deadlineDate?: string;
    // New deadline date in YYYY-MM-DD.
    // Use "remove" to clear the deadline.

    dueString?: string;
    // New due date in natural language.
    // Example: "tomorrow at 5pm"
    // Use "remove" to clear the due date.

    description?: string;
    // New additional details or notes.
    // Supports Markdown.

    parentId?: string;
    // New parent task ID.

    order?: number;
    // New order within parent/section.

    labels?: string[];
    // New labels. Replaces all existing labels.

    isUncompletable?: boolean;
    // Whether this task should be an organizational header.

    sectionId?: string;
    // New section ID.
  }>
})
```

---

## Sync API-backed tools

These tools require the Todoist Sync API, or are derived workflows that depend on Sync-only data/commands.

### `add-filters`

**Description:** Add one or more new personal filters. Filters are saved custom views using query syntax to organize tasks.

```ts
add-filters({
  filters: Array<{
    isFavorite?: boolean; // Defaults to false

    query: string; // Required, minLength: 1
    // Examples:
    // "today & p1"
    // "#Work & overdue"
    // "@email & today"
    // "(p1 | p2) & !assigned"
    // Operators: |, &, !, (), ,

    color:
      | "berry_red"
      | "red"
      | "orange"
      | "yellow"
      | "olive_green"
      | "lime_green"
      | "green"
      | "mint_green"
      | "teal"
      | "sky_blue"
      | "light_blue"
      | "blue"
      | "grape"
      | "violet"
      | "lavender"
      | "magenta"
      | "salmon"
      | "charcoal"
      | "grey"
      | "taupe";

    name: string; // Required, minLength: 1
  }>
})
```

---

### `delete-object`

**Description:** Delete a project, section, task, comment, label, filter, reminder, or location reminder by its ID.

**Backend note:** `type: "task"` maps to v1 `DELETE /api/v1/tasks/{task_id}`. Other object types require Sync/API-specific handling.

```ts
delete-object({
  type:
    | "project"
    | "section"
    | "task"
    | "comment"
    | "label"
    | "filter"
    | "reminder"
    | "location_reminder";

  id: string;
})
```

---

### `fetch`

**Description:** Fetch the full contents of a task or project by its ID. The ID should be in the format `task:{id}` or `project:{id}`.

```ts
fetch({
  id: string; // Required
  // Format:
  // "task:{id}"
  // "project:{id}"
})
```

---

### `fetch-object`

**Description:** Fetch a single task, project, comment, or section by its ID. Use this when you have a specific object ID and want to retrieve full details.

```ts
fetch-object({
  type: "task" | "project" | "comment" | "section";
  id: string;
})
```

---

### `find-filters`

**Description:** List all personal filters or search for filters by name. Filters are saved custom views using Todoist query syntax.

```ts
find-filters({
  search?: string;
  // Partial and case-insensitive match.
  // If omitted, all filters are returned.
})
```

---

### `find-tasks-by-date`

**Description:** Get tasks by date range. `startDate='today'` includes overdue items. Default `responsibleUserFiltering='unassignedOrMe'` excludes others' tasks. Person-specific queries require `responsibleUser`.

```ts
find-tasks-by-date({
  startDate?: string;
  // Format: YYYY-MM-DD or "today"

  daysCount?: number;
  // Default: 1
  // Minimum: 1
  // Maximum: 30

  overdueOption?: "overdue-only" | "include-overdue" | "exclude-overdue";
  // Default: "include-overdue"

  responsibleUser?: string;
  // User ID, name, or email.

  responsibleUserFiltering?: "assigned" | "unassignedOrMe" | "all";
  // Default: "unassignedOrMe"

  labels?: string[];

  labelsOperator?: "and" | "or";
  // Default: "or"

  limit?: number;
  // Default: 10
  // Minimum: 1
  // Maximum: 100

  cursor?: string;
})
```

---

### `get-overview`

**Description:** Get a Markdown overview. If no `projectId` is provided, shows all projects with hierarchy and sections. If `projectId` is provided, shows detailed overview of that specific project including all tasks grouped by sections.

```ts
get-overview({
  projectId?: string; // Optional, minLength: 1
})
```

---

### `get-project-activity-stats`

**Description:** Get daily and optional weekly task completion counts for a project over a configurable time window. Useful for identifying completion trends and patterns.

```ts
get-project-activity-stats({
  projectId: string; // Required

  weeks?: number;
  // Number of weeks of activity data to retrieve.
  // Default: 2
  // Minimum: 1
  // Maximum: 12

  includeWeeklyCounts?: boolean;
})
```

---

### `get-project-health`

**Description:** Get a comprehensive health assessment for a project including completion progress, health status, and optional detailed context with project metrics and task-level recommendations.

```ts
get-project-health({
  projectId: string; // Required

  includeContext?: boolean;
  // Default: false
  // If true, includes detailed health context with project metrics and task-level data.
  // May produce large output for projects with many tasks.
})
```

---

### `manage-assignments`

**Description:** Bulk assignment operations for multiple tasks. Supports assign, unassign, and reassign operations with atomic rollback on failures.

```ts
manage-assignments({
  taskIds: string[];
  // Required
  // Max 50 tasks

  operation: "assign" | "unassign" | "reassign";

  responsibleUser?: string;
  // Required for assign and reassign.
  // Can be "me", user ID, name, or email.

  fromAssigneeUser?: string;
  // For reassign operations.
  // Optional. If omitted, reassigns from any current assignee.

  dryRun?: boolean;
  // Default: false
  // If true, validates without executing.
})
```

---

### `reschedule-tasks`

**Description:** Reschedule tasks to new dates while preserving recurring schedules. Unlike `update-tasks`, which replaces the entire due string and can wipe recurrence, this changes only the date while keeping recurrence patterns intact.

```ts
reschedule-tasks({
  tasks: Array<{
    id: string;   // Required, minLength: 1

    date: string; // Required, minLength: 1
    // Use:
    // YYYY-MM-DD
    // or YYYY-MM-DDTHH:MM:SS
    //
    // If date-only is provided and the task already has a specific time,
    // the existing time is preserved.
  }>
})
```

---

### `search`

**Description:** Search across tasks and projects in Todoist. Returns a list of relevant results with IDs, titles, and URLs.

```ts
search({
  query: string; // Required
})
```
