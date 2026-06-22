"""Todoist tool specs, LangChain tool wrappers, and the back-compat dispatcher.

Domain-specific code only. The generic dispatcher, ToolNode bridge, registry, and
the ``ask_user`` control tool live in ``tools/dispatcher.py``, ``tools/base.py``,
and ``tools/control.py`` respectively; the names historically importable from this
module are re-exported below so existing imports keep working.
"""

from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import InjectedToolCallId, tool
from pydantic import BaseModel, ConfigDict, model_validator

from agents.agent_api.app.constants import ALLOW_MUTATIONS
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
        "delete_todoist_task": todoist_client.delete_todoist_task,
        "get_completed_todoist_tasks_by_completion_date": (
            todoist_client.get_completed_todoist_tasks_by_completion_date
        ),
    }
    return [
        ToolSpec(
            name=name,
            openai_schema=schemas[name],
            handler=handler,
            mutating=name in MUTATING_TOOL_NAMES,
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

    @tool(args_schema=UpdateTodoistTaskInput)
    def update_todoist_task(**arguments: Any) -> Dict[str, Any]:
        """Update an existing Todoist task."""

        tool_call_id = arguments.pop("tool_call_id")
        return dispatch(
            tool_call_id,
            "update_todoist_task",
            arguments,
        )

    @tool
    def complete_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Dict[str, Any]:
        """Mark a Todoist task complete."""

        return dispatch(tool_call_id, "complete_task", {"task_id": task_id})

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

    return [
        add_todoist_task,
        get_todoist_task,
        get_tasks,
        get_tasks_by_filter,
        update_todoist_task,
        complete_task,
        delete_todoist_task,
        get_completed_todoist_tasks_by_completion_date,
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
    ):
        self.todoist_client = todoist_client
        registry = ToolRegistry()
        registry.register(get_control_tool_specs())
        registry.register(
            get_todoist_tool_specs(todoist_client),
            langchain_builder=build_todoist_langchain_tools,
        )
        super().__init__(registry, allow_mutations=allow_mutations, tracer=tracer)


__all__ = [
    # Todoist-specific
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
