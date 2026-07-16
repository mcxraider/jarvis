"""Todoist tool specs, LangChain tool wrappers, and the back-compat dispatcher.

Domain-specific code only. The generic dispatcher, ToolNode bridge, registry, and
the ``ask_user`` control tool live in ``tools/dispatcher.py``, ``tools/base.py``,
and ``tools/control.py`` respectively; the names historically importable from this
module are re-exported below so existing imports keep working.
"""

from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import InjectedToolCallId, StructuredTool, tool
from pydantic import BaseModel, ConfigDict, model_validator

from agents.agent_api.app.constants import ALLOW_MUTATIONS
from agents.agent_api.app.idempotency.store import IdempotencyStore
from agents.agent_api.app.tools.base import (
    DispatchFn,
    ToolRegistry,
    ToolSpec,
    parse_tool_call_arguments,
    tool_call_name,
)
from agents.agent_api.app.tools.control import (
    ASK_USER_TOOL_NAME,
    get_control_tool_specs,
    is_ask_user_tool_call,
)
from agents.agent_api.app.tools.dispatcher import (
    ToolDispatcher,
    execute_tool_calls_with_toolnode,
    openai_tool_call_to_toolnode_call,
    tool_message_to_result,
    tool_result_to_message,
    toolnode_output_messages,
)
from agents.agent_api.app.tools.todoist.schemas import (
    MUTATING_TOOL_NAMES,
    get_todoist_tool_schemas,
)
from agents.agent_api.app.tracing import TracePrinter


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
- Pagination: a `next_cursor` field appears in results. If it is null, you have everything — stop. Only pass a cursor value received verbatim from a prior response.
- `get_projects` lists projects (pass `search` to filter by name substring). Use it to turn a project name into an `id` before adding a task there — this is a distinct step: find the project in one turn, then add the task by its id in the next (see Grounding).
- `create_project` makes a NEW project (only `name` is required). A single create runs without a confirmation prompt — do NOT add your own "are you sure?"; just issue the call. Only create a project when the user clearly asks for a new one; otherwise search existing projects first."""


class UpdateTodoistTaskInput(BaseModel):
    """Validated update arguments that retain which nullable fields were supplied."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    content: Optional[str] = None
    description: Optional[str] = None
    labels: Optional[List[str]] = None
    priority: Optional[int] = None
    due_string: Optional[str] = None
    due_date: Optional[str] = None
    due_datetime: Optional[str] = None
    due_lang: Optional[str] = None
    assignee_id: Optional[int] = None
    duration: Optional[int] = None
    duration_unit: Optional[str] = None
    deadline_date: Optional[str] = None
    child_order: Optional[int] = None
    is_collapsed: Optional[bool] = None
    day_order: Optional[int] = None
    tool_call_id: Annotated[str, InjectedToolCallId]

    @model_validator(mode="after")
    def validate_duration_pair(self) -> "UpdateTodoistTaskInput":
        if "labels" in self.model_fields_set and self.labels is None:
            raise ValueError("labels must be an array of labels")
        if ("duration" in self.model_fields_set) != ("duration_unit" in self.model_fields_set):
            raise ValueError("duration and duration_unit must be provided together")
        if self.duration is not None and self.duration_unit is None:
            raise ValueError("duration_unit is required when duration is set")
        if self.duration_unit is not None and self.duration is None:
            raise ValueError("duration is required when duration_unit is set")
        if self.duration is not None and self.duration <= 0:
            raise ValueError("duration must be a positive integer")
        if self.duration_unit is not None and self.duration_unit not in {"minute", "day"}:
            raise ValueError("duration_unit must be minute or day")
        return self


class SuppliedFieldsStructuredTool(StructuredTool):
    """Keep optional schema defaults from becoming synthetic tool arguments.

    Supplied fields are derived from the raw ``tool_input`` (what the model actually
    sent) plus the injected ``tool_call_id`` — deliberately NOT from any langchain
    internal. An earlier version consulted ``self._injected_args_keys``, which exists
    only in newer langchain-core; on the pinned version it is absent and every update
    raised ``AttributeError``. Deriving supplied fields locally keeps this
    version-agnostic.
    """

    def _parse_input(
        self,
        tool_input: str | Dict[str, Any],
        tool_call_id: Optional[str],
    ) -> str | Dict[str, Any]:
        supplied_fields = set(tool_input) if isinstance(tool_input, dict) else set()
        parsed = super()._parse_input(tool_input, tool_call_id)
        if not isinstance(parsed, dict):
            return parsed
        supplied_fields.add("tool_call_id")
        return {key: value for key, value in parsed.items() if key in supplied_fields}


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
    # Async support is additive: production Todoist clients expose every native
    # handler, while legacy/injected sync-only clients remain valid and can be
    # offloaded by the later async dispatcher compatibility path.
    async_handlers = {
        name: getattr(todoist_client, f"async_{name}", None)
        for name in handlers
    }
    return [
        ToolSpec(
            name=name,
            openai_schema=schemas[name],
            handler=handler,
            mutating=name in MUTATING_TOOL_NAMES,
            async_handler=async_handlers[name],
        )
        for name, handler in handlers.items()
    ]


def build_todoist_langchain_tools(dispatch: DispatchFn) -> List[Any]:
    """Build LangChain tool wrappers that delegate to the shared dispatch pipeline.

    ``dispatch(tool_call_id, tool_name, arguments)`` runs the call through the
    dispatcher's mutation guard, result envelope, and tracing.
    """

    @tool
    def add_todoist_task(
        content: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        description: Optional[str] = None,
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        order: Optional[int] = None,
        labels: Optional[List[str]] = None,
        priority: Optional[int] = None,
        due_string: Optional[str] = None,
        due_date: Optional[str] = None,
        due_datetime: Optional[str] = None,
        due_lang: Optional[str] = None,
        assignee_id: Optional[int] = None,
        duration: Optional[int] = None,
        duration_unit: Optional[str] = None,
        deadline_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Todoist task."""

        return dispatch(
            tool_call_id,
            "add_todoist_task",
            {
                "content": content,
                "description": description,
                "project_id": project_id,
                "section_id": section_id,
                "parent_id": parent_id,
                "order": order,
                "labels": labels,
                "priority": priority,
                "due_string": due_string,
                "due_date": due_date,
                "due_datetime": due_datetime,
                "due_lang": due_lang,
                "assignee_id": assignee_id,
                "duration": duration,
                "duration_unit": duration_unit,
                "deadline_date": deadline_date,
            },
        )

    @tool
    def get_todoist_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Get one Todoist task by ID."""

        return dispatch(tool_call_id, "get_todoist_task", {"task_id": task_id})

    @tool
    def get_tasks(
        tool_call_id: Annotated[str, InjectedToolCallId],
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        label: Optional[str] = None,
        ids: Optional[List[str]] = None,
        goal_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List active Todoist tasks with optional filters."""

        return dispatch(
            tool_call_id,
            "get_tasks",
            {
                "project_id": project_id,
                "section_id": section_id,
                "parent_id": parent_id,
                "label": label,
                "ids": ids,
                "goal_id": goal_id,
                "cursor": cursor,
                "limit": limit,
            },
        )

    @tool
    def get_tasks_by_filter(
        query: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        lang: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List active Todoist tasks matching a filter expression."""

        return dispatch(
            tool_call_id,
            "get_tasks_by_filter",
            {"query": query, "lang": lang, "cursor": cursor, "limit": limit},
        )

    def _update_todoist_task(**arguments: Any) -> Dict[str, Any]:
        """Update an existing Todoist task."""

        tool_call_id = arguments.pop("tool_call_id")
        return dispatch(
            tool_call_id,
            "update_todoist_task",
            arguments,
        )

    update_todoist_task = SuppliedFieldsStructuredTool.from_function(
        func=_update_todoist_task,
        name="update_todoist_task",
        description="Update an existing Todoist task.",
        args_schema=UpdateTodoistTaskInput,
    )

    @tool
    def complete_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Mark a Todoist task complete."""

        return dispatch(tool_call_id, "complete_task", {"task_id": task_id})

    @tool
    def uncomplete_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Uncomplete (reopen) a completed Todoist task."""

        return dispatch(tool_call_id, "uncomplete_task", {"task_id": task_id})

    @tool
    def delete_todoist_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Delete a Todoist task permanently."""

        return dispatch(tool_call_id, "delete_todoist_task", {"task_id": task_id})

    @tool
    def get_completed_todoist_tasks_by_completion_date(
        tool_call_id: Annotated[str, InjectedToolCallId],
        since: Optional[str] = None,
        until: Optional[str] = None,
        workspace_id: Optional[int] = None,
        project_id: Optional[str] = None,
        section_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        filter_query: Optional[str] = None,
        filter_lang: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List completed Todoist tasks by completion date."""

        return dispatch(
            tool_call_id,
            "get_completed_todoist_tasks_by_completion_date",
            {
                "since": since,
                "until": until,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "section_id": section_id,
                "parent_id": parent_id,
                "filter_query": filter_query,
                "filter_lang": filter_lang,
                "cursor": cursor,
                "limit": limit,
            },
        )

    @tool
    def get_comments(
        tool_call_id: Annotated[str, InjectedToolCallId],
        task_id: Optional[str] = None,
        project_id: Optional[str] = None,
        comment_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List comments on a task or project, or fetch a single comment by ID."""

        return dispatch(
            tool_call_id,
            "get_comments",
            {
                "task_id": task_id,
                "project_id": project_id,
                "comment_id": comment_id,
                "cursor": cursor,
                "limit": limit,
            },
        )

    @tool
    def add_comment(
        content: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        task_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a comment to a task or project."""

        return dispatch(
            tool_call_id,
            "add_comment",
            {"content": content, "task_id": task_id, "project_id": project_id},
        )

    @tool
    def get_labels(
        tool_call_id: Annotated[str, InjectedToolCallId],
        search: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List the user's personal labels, optionally filtered by name."""

        return dispatch(
            tool_call_id,
            "get_labels",
            {"search": search, "cursor": cursor, "limit": limit},
        )

    @tool
    def get_projects(
        tool_call_id: Annotated[str, InjectedToolCallId],
        search: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """List the user's Todoist projects, optionally filtered by name."""

        return dispatch(
            tool_call_id,
            "get_projects",
            {"search": search, "cursor": cursor, "limit": limit},
        )

    @tool
    def create_project(
        name: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        description: Optional[str] = None,
        parent_id: Optional[str] = None,
        color: Optional[str] = None,
        is_favorite: Optional[bool] = None,
        view_style: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new Todoist project."""

        return dispatch(
            tool_call_id,
            "create_project",
            {
                "name": name,
                "description": description,
                "parent_id": parent_id,
                "color": color,
                "is_favorite": is_favorite,
                "view_style": view_style,
            },
        )

    return [
        add_todoist_task,
        get_todoist_task,
        get_tasks,
        get_tasks_by_filter,
        update_todoist_task,
        complete_task,
        uncomplete_task,
        delete_todoist_task,
        get_completed_todoist_tasks_by_completion_date,
        get_comments,
        add_comment,
        get_labels,
        get_projects,
        create_project,
    ]


class TodoistToolDispatcher(ToolDispatcher):
    """Backward-compatible dispatcher constructed directly from a Todoist client.

    New code should build a :class:`ToolRegistry` and a generic
    :class:`ToolDispatcher`; this subclass preserves the historical
    ``TodoistToolDispatcher(client, ...)`` construction used by tests and callers.
    """

    def __init__(
        self,
        todoist_client: Any,
        allow_mutations: bool = ALLOW_MUTATIONS,
        tracer: Optional[TracePrinter] = None,
        idempotency_store: Optional[IdempotencyStore] = None,
        **idempotency_options: Any,
    ):
        self.todoist_client = todoist_client
        registry = ToolRegistry()
        registry.register(get_control_tool_specs())
        registry.register(
            get_todoist_tool_specs(todoist_client),
            langchain_builder=build_todoist_langchain_tools,
        )
        super().__init__(
            registry,
            allow_mutations=allow_mutations,
            tracer=tracer,
            idempotency_store=idempotency_store,
            **idempotency_options,
        )


__all__ = [
    # Todoist-specific
    "TODOIST_GROUNDING_NOTE",
    "TODOIST_PROMPT_FRAGMENT",
    "TodoistToolDispatcher",
    "UpdateTodoistTaskInput",
    "build_todoist_langchain_tools",
    "get_todoist_tool_specs",
    # Re-exported control + generic helpers (historical import surface)
    "ASK_USER_TOOL_NAME",
    "execute_tool_calls_with_toolnode",
    "is_ask_user_tool_call",
    "openai_tool_call_to_toolnode_call",
    "parse_tool_call_arguments",
    "tool_call_name",
    "tool_message_to_result",
    "tool_result_to_message",
    "toolnode_output_messages",
]
