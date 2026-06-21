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
    "delete_todoist_task",
}


def get_todoist_tool_schemas() -> List[Dict[str, Any]]:
    """Return the Todoist-only OpenAI/DeepSeek function tool schemas."""

    add_task_parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Task title or content"},
            "description": {"type": "string", "description": "Optional task details"},
            "project_id": {"type": "string", "description": "Project ID"},
            "section_id": {"type": "string", "description": "Section ID"},
            "parent_id": {"type": "string", "description": "Parent task ID"},
            "order": {"type": "integer", "description": "Task order"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "priority": {
                "type": "integer",
                "enum": [1, 2, 3, 4],
                "description": "1 normal, 2 low, 3 medium, 4 high",
            },
            "due_string": {"type": "string", "description": "Natural due date"},
            "due_date": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "YYYY-MM-DD due date",
            },
            "due_datetime": {"type": "string", "description": "RFC3339 due datetime"},
            "due_lang": {"type": "string", "description": "Due date language code"},
            "assignee_id": {"type": "integer", "description": "Numeric assignee user ID"},
            "duration": {"type": "integer", "minimum": 1, "description": "Task duration"},
            "duration_unit": {
                "type": "string",
                "enum": ["minute", "day"],
                "description": "Duration unit; required with duration",
            },
            "deadline_date": {
                "type": "string",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "description": "YYYY-MM-DD deadline",
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
            "task_id": {"type": "string", "description": "Todoist task ID"},
            "content": {"type": "string", "description": "New task title"},
            "description": {"type": "string", "description": "New task details"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
            "due_string": {"type": "string", "description": "Natural due date"},
            "due_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
            "due_datetime": {"type": "string", "description": "RFC3339 due datetime"},
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
            "project_id": {"type": "string"},
            "section_id": {"type": "string"},
            "parent_id": {"type": "string"},
            "label": {"type": "string"},
            "ids": {"type": "array", "items": {"type": "string"}},
            "goal_id": {"type": "string", "format": "uuid"},
            "cursor": {"type": "string", "description": "Pagination cursor"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
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
                    "Todoist filter expression; comma-separated multi-list filters "
                    "are unsupported"
                ),
            },
            "lang": {"type": "string", "description": "IETF filter language tag"},
            "cursor": {"type": "string", "description": "Pagination cursor"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    completed_tasks_parameters = {
        "type": "object",
        "properties": {
            "since": {"type": "string", "format": "date-time", "description": "RFC3339 start"},
            "until": {"type": "string", "format": "date-time", "description": "RFC3339 end"},
            "workspace_id": {"type": "integer", "minimum": 1},
            "project_id": {"type": "string"},
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
            "cursor": {"type": "string", "description": "Pagination cursor"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": [],
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
                "description": "Mark a Todoist task complete.",
                "parameters": task_id_parameters,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_todoist_task",
                "description": "Delete a Todoist task permanently.",
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
