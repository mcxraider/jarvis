"""Todoist tool schemas exposed to the LLM.

These OpenAI/DeepSeek-compatible function schemas are the contract the model sees
when deciding whether to call a Todoist function and what arguments to provide.
"""

from typing import Any, Dict, List

ASK_USER_TOOL_NAME = "ask_user"

MUTATING_TOOL_NAMES = {
    "add_todoist_task",
    "update_todoist_task",
    "complete_task",
    "delete_todoist_task",
}


def get_todoist_tools() -> List[Dict[str, Any]]:
    """Return OpenAI/DeepSeek-compatible function tool schemas."""

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
            "assignee_id": {"type": "string", "description": "Assignee user ID"},
        },
        "required": ["content"],
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
            "assignee_id": {"type": "string", "description": "Assignee user ID"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }

    get_tasks_parameters = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "section_id": {"type": "string"},
            "label": {"type": "string"},
            "filter": {
                "type": "string",
                "description": "Todoist filter expression, e.g. today, overdue, p1",
            },
            "lang": {"type": "string", "description": "Language code"},
            "ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": [],
        "additionalProperties": False,
    }

    completed_tasks_parameters = {
        "type": "object",
        "properties": {
            "since": {"type": "string", "description": "ISO 8601 start date"},
            "until": {"type": "string", "description": "ISO 8601 end date"},
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

    ask_user_parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "One concise question to ask the user before continuing.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short explanation of why clarification is needed.",
            },
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional missing inputs needed to continue safely.",
            },
            "risk": {
                "type": "string",
                "description": "Optional risk if Jarvis guessed instead of asking.",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    return [
        {
            "type": "function",
            "function": {
                "name": ASK_USER_TOOL_NAME,
                "description": (
                    "Ask the user for one missing or risky detail. This pauses the "
                    "LangGraph run with a human-in-the-loop interrupt."
                ),
                "parameters": ask_user_parameters,
            },
        },
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
                "description": "List active Todoist tasks with optional filters.",
                "parameters": get_tasks_parameters,
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


__all__ = ["ASK_USER_TOOL_NAME", "MUTATING_TOOL_NAMES", "get_todoist_tools"]
