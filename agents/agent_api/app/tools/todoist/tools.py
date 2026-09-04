"""Todoist tool specs and prompt contributions.

Domain-specific code only. The generic dispatcher, registry, and the
``ask_user`` control tool live in ``tools/dispatcher.py``, ``tools/base.py``,
and ``tools/control.py`` respectively.
"""

import inspect
from typing import Any, Dict, List, Optional

from agents.agent_api.app.tools.base import ToolSpec
from agents.agent_api.app.tools.todoist.schemas import (
    MUTATING_TOOL_NAMES,
    get_todoist_tool_schemas,
)


# --- Prompt contributions -----------------------------------------------------
# A domain owns its own prompt text so the orchestrator never has to know Todoist
# exists. ``domain_adapters.py`` wires these onto the Todoist DomainAdapter, and
# the prompt composer appends the fragment (and the grounding note) only when the
# domain is active for this user. Adding a domain is one adapter entry — no edits
# to the orchestrator prompt.

TODOIST_GROUNDING_NOTE = (
    "Todoist: mutations (`update_todoist_task`, `complete_task`, `uncomplete_task`, "
    "`delete_todoist_task`, `add_comment`) require a real `task_id` returned by a prior "
    "read (`get_tasks`, `get_tasks_by_filter`, `get_todoist_task`) in this same "
    "conversation. The same applies to `project_id`: to route a task into a named "
    "project, call `get_projects` first to resolve the name to its id, THEN "
    "`add_todoist_task` with that id in a SEPARATE turn — never guess a `project_id`; "
    "omit it to use the Inbox."
)

TODOIST_PROMPT_FRAGMENT = """\
## Todoist tool tips
- Creating many tasks at once → issue one `add_todoist_task` call per task. The system batches and gates them for you.
- Dates: prefer `due_string` ("2026-07-02 3pm", "tomorrow 9am") — but always pre-resolve relative dates per the rule above.
- Priority format: 4 = urgent/P1, 3 = high/P2, 2 = medium/P3, 1 = normal/P4 (default).
- `get_tasks_by_filter` takes Todoist filter syntax, NOT free text. To match by title use the `search:` operator (e.g. `search: dentist`) — do not pass a bare title like "dentist appointment" as the filter. Date ranges use "due after: X & due before: Y" — never a slash, dash, or "between". Always include the 4-digit year in absolute dates (e.g. "Jul 19 2026", not "Jul 19") — Todoist silently rolls year-free past dates to the following year, breaking date ranges. Examples: "today", "overdue", "p1", "7 days", "search: groceries", "due after: Jul 5 2026 & due before: Jul 13 2026".
- After scheduling a task that has a specific time, check for clashes with other timed tasks that day; if any overlap, tell the user and ask whether to reschedule.
- Never fabricate task IDs — fetch first (see Grounding).
- Do not retry `add_todoist_task` on timeout — it may have succeeded. Verify with `get_tasks_by_filter` to avoid duplicates.
- Pagination: collection reads return one page (50 items by default; comments default to their maximum of 10). A `next_cursor` field appears in results. If it is null, you have everything — stop. Only pass a cursor value received verbatim from a prior response.
- `get_projects` lists projects (pass `search` to filter by name substring). Use it to turn a project name into an `id` before adding a task there — this is a distinct step: find the project in one turn, then add the task by its id in the next (see Grounding).
- `create_project` makes a NEW project (only `name` is required). A single create runs without a confirmation prompt — do NOT add your own "are you sure?"; just issue the call. Only create a project when the user clearly asks for a new one; otherwise search existing projects first."""


def get_todoist_tool_specs(todoist_client: Any) -> List[ToolSpec]:
    """Build one :class:`ToolSpec` per Todoist tool, pairing schema + client method.

    This is the single place a Todoist tool is declared for the registry: schema
    (from ``schemas.py``), handler (the client method), and the mutation flag.
    """

    schemas = {schema["function"]["name"]: schema for schema in get_todoist_tool_schemas()}
    handlers = {
        "add_todoist_task": todoist_client.add_todoist_task,
        "get_todoist_task": todoist_client.get_todoist_task,
        "get_tasks": todoist_client.get_tasks,
        "get_tasks_by_filter": todoist_client.get_tasks_by_filter,
        "update_todoist_task": todoist_client.update_todoist_task,
        "complete_task": todoist_client.complete_task,
        "uncomplete_task": todoist_client.uncomplete_task,
        "delete_todoist_task": todoist_client.delete_todoist_task,
        "get_completed_todoist_tasks_by_completion_date": (
            todoist_client.get_completed_todoist_tasks_by_completion_date
        ),
        "get_comments": todoist_client.get_comments,
        "add_comment": todoist_client.add_comment,
        "get_labels": todoist_client.get_labels,
        "get_projects": todoist_client.get_projects,
        "create_project": todoist_client.create_project,
    }
    async_handlers = {
        name: candidate
        for name in handlers
        if inspect.iscoroutinefunction(
            candidate := getattr(todoist_client, f"async_{name}", None)
        )
    }
    return [
        ToolSpec(
            name=name,
            openai_schema=schemas[name],
            handler=handler,
            mutating=name in MUTATING_TOOL_NAMES,
            async_handler=async_handlers.get(name),
        )
        for name, handler in handlers.items()
    ]


__all__ = [
    "TODOIST_GROUNDING_NOTE",
    "TODOIST_PROMPT_FRAGMENT",
    "get_todoist_tool_specs",
]
