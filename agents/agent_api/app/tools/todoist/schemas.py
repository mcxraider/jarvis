"""Todoist tool schemas exposed to the LLM.

These OpenAI/DeepSeek-compatible function schemas are the contract the model sees
when deciding whether to call a Todoist function and what arguments to provide.

The generic ``ask_user`` control pseudo-tool lives in ``tools/control.py``;
``ASK_USER_TOOL_NAME`` is re-exported here for backward compatibility.
"""

from typing import Any, Dict, List

from agents.agent_api.app.tools.control import ASK_USER_TOOL_NAME, get_control_tools

MUTATING_TOOL_NAMES = {
    "add_todoist_task",
    "update_todoist_task",
    "complete_task",
    "uncomplete_task",
    "delete_todoist_task",
    "add_comment",
    "create_project",
}


def get_todoist_tool_schemas() -> List[Dict[str, Any]]:
    """Return the Todoist-only OpenAI/DeepSeek function tool schemas."""

    add_task_parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "Task name/title. Should be concise and actionable. Supports Markdown."
                ),
            },
            "description": {
                "type": "string",
                "description": "Additional details or notes for the task. Supports Markdown.",
            },
            "project_id": {
                "type": "string",
                "description": "Project ID to add the task to. Omit to use the Inbox.",
            },
            "section_id": {
                "type": "string",
                "description": "Section ID within the project to place the task under.",
            },
            "parent_id": {
                "type": "string",
                "description": "Parent task ID. Set this to create the task as a subtask.",
            },
            "order": {
                "type": "integer",
                "description": "Position of the task among its siblings (lower appears first).",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Label names to attach to the task.",
            },
            "priority": {
                "type": "integer",
                "enum": [1, 2, 3, 4],
                "description": (
                    "Task priority as an integer 1-4. 4 = highest urgency (shown as P1 in "
                    "the Todoist UI), 3 = P2, 2 = P3, 1 = normal/default (P4). Higher number "
                    "means more urgent."
                ),
            },
            "due_string": {
                "type": "string",
                "description": (
                    "Natural-language due date, e.g. 'tomorrow at 3pm', 'every Monday'. "
                    "Use this for recurring tasks."
                ),
            },
            "due_date": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "All-day due date in YYYY-MM-DD format.",
            },
            "due_datetime": {
                "type": "string",
                "description": "Due date and time as an RFC3339 datetime (e.g. 2025-12-31T17:00:00Z).",
            },
            "due_lang": {
                "type": "string",
                "description": "Language code used to parse due_string (e.g. 'en').",
            },
            "assignee_id": {
                "type": "integer",
                "description": (
                    "Numeric user ID to assign the task to. The user must be a collaborator "
                    "on the target project."
                ),
            },
            "duration": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Amount of time the task takes, paired with duration_unit. Max 24 hours "
                    "(1440 minutes). Requires duration_unit."
                ),
            },
            "duration_unit": {
                "type": "string",
                "enum": ["minute", "day"],
                "description": "Unit for duration; required when duration is set.",
            },
            "deadline_date": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": (
                    "Deadline date in YYYY-MM-DD format, e.g. '2025-12-31'. Distinct from "
                    "the due date."
                ),
            },
        },
        "required": ["content"],
        "dependentRequired": {
            "duration": ["duration_unit"],
            "duration_unit": ["duration"],
        },
        "additionalProperties": False,
    }

    task_id_parameters = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "Todoist task ID"}},
        "required": ["task_id"],
        "additionalProperties": False,
    }

    update_task_parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Todoist task ID to update"},
            "content": {
                "type": "string",
                "description": "New task name/title. Concise and actionable; supports Markdown.",
            },
            "description": {
                "type": "string",
                "description": "New additional details or notes. Supports Markdown.",
            },
            "labels": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New labels. Replaces all existing labels on the task.",
            },
            "priority": {
                "type": "integer",
                "enum": [1, 2, 3, 4],
                "description": (
                    "Task priority as an integer 1-4. 4 = highest urgency (shown as P1 in "
                    "the Todoist UI), 3 = P2, 2 = P3, 1 = normal/default (P4). Higher number "
                    "means more urgent."
                ),
            },
            "due_string": {
                "type": "string",
                "description": (
                    "New natural-language due date, e.g. 'tomorrow at 5pm'. Use for "
                    "recurring schedules."
                ),
            },
            "due_date": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "New all-day due date in YYYY-MM-DD format.",
            },
            "due_datetime": {
                "type": "string",
                "description": "New due date and time as an RFC3339 datetime.",
            },
            "due_lang": {"type": "string", "description": "Due date language code"},
            "assignee_id": {
                "type": ["integer", "null"],
                "description": "Numeric assignee user ID; null clears assignment",
            },
            "duration": {
                "type": ["integer", "null"],
                "minimum": 1,
                "description": "Task duration; null clears duration",
            },
            "duration_unit": {
                "type": ["string", "null"],
                "enum": ["minute", "day", None],
                "description": "Duration unit; null clears duration",
            },
            "deadline_date": {
                "type": ["string", "null"],
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "YYYY-MM-DD deadline; null clears deadline",
            },
            "child_order": {"type": "integer", "description": "Position in current scope"},
            "is_collapsed": {"type": "boolean", "description": "Collapsed state"},
            "day_order": {"type": "integer", "description": "Today/Upcoming position"},
        },
        "required": ["task_id"],
        "dependentRequired": {
            "duration": ["duration_unit"],
            "duration_unit": ["duration"],
        },
        "additionalProperties": False,
    }

    get_tasks_parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Only return active tasks in this project.",
            },
            "section_id": {
                "type": "string",
                "description": "Only return active tasks in this section.",
            },
            "parent_id": {
                "type": "string",
                "description": "Only return subtasks of this parent task.",
            },
            "label": {
                "type": "string",
                "description": "Only return tasks carrying this label name.",
            },
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fetch only these specific task IDs.",
            },
            "goal_id": {"type": "string", "format": "uuid"},
            "cursor": {
                "type": ["string", "null"],
                "description": (
                    "Opaque pagination token from a previous response's next_cursor field. "
                    "Omit or pass null for the first page. NEVER fabricate a cursor value."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
                "description": "Maximum tasks in this page. Defaults to 50.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    get_tasks_by_filter_parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
                "description": (
                    "Todoist filter expression. "
                    "Date filters: 'today', 'tomorrow', 'overdue', 'next week', '7 days', "
                    "'due: Jul 6 2026', 'due before: tomorrow', "
                    "'due after: Jul 5 2026 & due before: Jul 13 2026' (date range). "
                    "IMPORTANT: always include the 4-digit year in absolute dates — "
                    "a year-free date like 'Jul 12' silently rolls to the next year if that "
                    "date is already past, producing an impossible range that returns no results. "
                    "Other: 'p1'-'p4' (priority), '#ProjectName' (project), '@label' (label), "
                    "'search: keyword' (text search), 'no due date', 'recurring'. "
                    "Operators: & (and), | (or), ! (not), () grouping. "
                    "INVALID patterns that will 400: slash ranges 'X / Y', dash ranges 'X - Y', "
                    "'between X and Y', >=/<= operators, 'this week'. "
                    "For date ranges, always use 'due after: X & due before: Y'."
                ),
            },
            "lang": {"type": "string", "description": "IETF filter language tag"},
            "cursor": {
                "type": ["string", "null"],
                "description": (
                    "Opaque pagination token from a previous response's next_cursor field. "
                    "Omit or pass null for the first page. NEVER fabricate a cursor value."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
                "description": "Maximum tasks in this page. Defaults to 50.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    completed_tasks_parameters = {
        "type": "object",
        "properties": {
            "since": {
                "type": "string",
                "format": "date-time",
                "description": (
                    "Start of the completion window as a timezone-aware RFC3339 timestamp "
                    "(e.g. 2025-12-01T00:00:00Z). Defaults to 30 days before 'until'. The "
                    "range cannot exceed three months."
                ),
            },
            "until": {
                "type": "string",
                "format": "date-time",
                "description": (
                    "End of the completion window as a timezone-aware RFC3339 timestamp. "
                    "Defaults to now."
                ),
            },
            "workspace_id": {"type": "integer", "minimum": 1},
            "project_id": {
                "type": "string",
                "description": "Only return completed tasks from this project.",
            },
            "section_id": {"type": "string"},
            "parent_id": {"type": "string"},
            "filter_query": {
                "type": "string",
                "description": "Todoist filter query to limit completed tasks",
            },
            "filter_lang": {
                "type": "string",
                "description": "Language code used to parse filter_query",
            },
            "cursor": {
                "type": ["string", "null"],
                "description": (
                    "Opaque pagination token from a previous response's next_cursor field. "
                    "Omit or pass null for the first page. NEVER fabricate a cursor value."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
                "description": "Maximum completed tasks in this page. Defaults to 50.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    get_comments_parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Return comments on this task. Provide a task or a project.",
            },
            "project_id": {
                "type": "string",
                "description": "Return comments on this project. Provide a task or a project.",
            },
            "comment_id": {
                "type": "string",
                "description": "Fetch a single comment by its ID instead of listing comments.",
            },
            "cursor": {
                "type": ["string", "null"],
                "description": (
                    "Opaque pagination token from a previous response's next_cursor field. "
                    "Omit or pass null for the first page. NEVER fabricate a cursor value."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 10,
                "description": "Maximum comments in this page. Defaults to 10.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    add_comment_parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "Comment text. Supports Markdown.",
            },
            "task_id": {
                "type": "string",
                "description": "ID of the task to comment on. Provide exactly one of task or project.",
            },
            "project_id": {
                "type": "string",
                "description": (
                    "ID of the project to comment on. Provide exactly one of task or project."
                ),
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    get_labels_parameters = {
        "type": "object",
        "properties": {
            "search": {
                "type": "string",
                "description": (
                    "Optional case-insensitive substring to filter labels by name "
                    "(e.g. 'work' matches 'work', 'homework'). Omit to return all labels."
                ),
            },
            "cursor": {
                "type": ["string", "null"],
                "description": (
                    "Opaque pagination token from a previous response's next_cursor field. "
                    "Omit or pass null for the first page. NEVER fabricate a cursor value."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
                "description": "Maximum labels in this page. Defaults to 50.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    get_projects_parameters = {
        "type": "object",
        "properties": {
            "search": {
                "type": "string",
                "description": (
                    "Optional case-insensitive substring to filter projects by name "
                    "(e.g. 'work' matches 'Work', 'Homework'). Omit to return all projects."
                ),
            },
            "cursor": {
                "type": ["string", "null"],
                "description": (
                    "Opaque pagination token from a previous response's next_cursor field. "
                    "Omit or pass null for the first page. NEVER fabricate a cursor value."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
                "description": "Maximum projects in this page. Defaults to 50.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    create_project_parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Name of the new project.",
            },
            "description": {
                "type": "string",
                "description": "Optional description for the project. Supports Markdown.",
            },
            "parent_id": {
                "type": "string",
                "description": "Parent project ID. Set this to nest the project as a sub-project.",
            },
            "color": {
                "type": "string",
                "description": (
                    "Color name for the project (e.g. 'berry_red', 'blue', 'green'). "
                    "Omit to use the Todoist default."
                ),
            },
            "is_favorite": {
                "type": "boolean",
                "description": "Whether to mark the project as a favorite.",
            },
            "view_style": {
                "type": "string",
                "enum": ["list", "board"],
                "description": "How the project is displayed in Todoist: 'list' or 'board'.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    return [
        {
            "type": "function",
            "function": {
                "name": "add_todoist_task",
                "description": "Create a Todoist task.",
                "parameters": add_task_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_todoist_task",
                "description": "Get one Todoist task by ID.",
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_tasks",
                "description": "List active Todoist tasks using structured fields and pagination.",
                "parameters": get_tasks_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_tasks_by_filter",
                "description": "List active Todoist tasks matching a filter expression.",
                "parameters": get_tasks_by_filter_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_todoist_task",
                "description": "Update an existing Todoist task.",
                "parameters": update_task_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "complete_task",
                "description": "Mark (close) a Todoist task as complete by its ID.",
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "uncomplete_task",
                "description": "Uncomplete (reopen) a previously completed Todoist task by its ID.",
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_todoist_task",
                "description": (
                    "Permanently delete a Todoist task by its ID. This cannot be undone."
                ),
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_completed_todoist_tasks_by_completion_date",
                "description": "List completed Todoist tasks by completion date.",
                "parameters": completed_tasks_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_comments",
                "description": (
                    "List comments on a task or project, or fetch a single comment by ID. "
                    "Provide task_id or project_id to list, or comment_id to fetch one."
                ),
                "parameters": get_comments_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_comment",
                "description": (
                    "Add a comment to a task or project. Provide exactly one of task_id "
                    "or project_id."
                ),
                "parameters": add_comment_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_labels",
                "description": (
                    "List the user's personal labels, optionally filtered by a name "
                    "substring."
                ),
                "parameters": get_labels_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_projects",
                "description": (
                    "List the user's Todoist projects, optionally filtered by a name "
                    "substring. Use this to resolve a project name to its ID before "
                    "adding a task to that project."
                ),
                "parameters": get_projects_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_project",
                "description": "Create a new Todoist project.",
                "parameters": create_project_parameters,
            },
        },
    ]


def get_todoist_tools() -> List[Dict[str, Any]]:
    """Return the full LLM tool list: control pseudo-tools then Todoist tools.

    Preserved for backward compatibility. New code should read the tool list from
    the :class:`ToolRegistry` (``registry.openai_schemas()``) instead.
    """

    return get_control_tools() + get_todoist_tool_schemas()


__all__ = [
    "ASK_USER_TOOL_NAME",
    "MUTATING_TOOL_NAMES",
    "get_todoist_tool_schemas",
    "get_todoist_tools",
]
