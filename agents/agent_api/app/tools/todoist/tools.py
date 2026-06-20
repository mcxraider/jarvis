"""Todoist tool dispatcher, LangChain tool wrappers, and tool-call helpers."""

import json
from typing import Annotated, Any, Dict, List, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import ToolNode
from langsmith import traceable

from agents.agent_api.app.constants import ALLOW_MUTATIONS
from agents.agent_api.app.tools.todoist.schemas import ASK_USER_TOOL_NAME, MUTATING_TOOL_NAMES
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


class TodoistToolDispatcher:
    """Bridge model tool calls to real Todoist client methods."""

    def __init__(
        self,
        todoist_client: Any,
        allow_mutations: bool = ALLOW_MUTATIONS,
        tracer: Optional[TracePrinter] = None,
    ):
        self.todoist_client = todoist_client
        self.allow_mutations = allow_mutations
        self.tracer = tracer or NULL_TRACE
        self.supported_tools = {
            "add_todoist_task": todoist_client.add_todoist_task,
            "get_todoist_task": todoist_client.get_todoist_task,
            "get_tasks": todoist_client.get_tasks,
            "update_todoist_task": todoist_client.update_todoist_task,
            "complete_task": todoist_client.complete_task,
            "delete_todoist_task": todoist_client.delete_todoist_task,
            "get_completed_todoist_tasks_by_completion_date": (
                todoist_client.get_completed_todoist_tasks_by_completion_date
            ),
        }

    def execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.tracer.event("tools.batch", "Executing tool call batch.", count=len(tool_calls))
        return [self.execute_tool_call(tool_call) for tool_call in tool_calls]

    def execute_tool_call(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        function_data = tool_call.get("function", {})
        tool_name = function_data.get("name", "unknown")

        try:
            arguments = self._parse_arguments(function_data.get("arguments", "{}"))
        except Exception as error:
            self.tracer.event("tool.error", "Tool call failed.", name=tool_name, error=str(error))
            return self._result(
                tool_call_id,
                tool_name,
                success=False,
                error=str(error),
            )

        return self.execute_tool(tool_call_id, tool_name, arguments)

    @traceable(
        name="todoist_execute_tool",
        run_type="tool",
        process_inputs=lambda inputs: {
            "tool_call_id": inputs.get("tool_call_id"),
            "tool_name": inputs.get("tool_name"),
            "argument_keys": sorted((inputs.get("arguments") or {}).keys()),
        },
    )
    def execute_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute one parsed tool call and return the Jarvis result envelope."""

        try:
            self.tracer.event(
                "tool.start",
                "Preparing tool call.",
                id=tool_call_id,
                name=tool_name,
                mutating=tool_name in MUTATING_TOOL_NAMES,
            )
            self.tracer.payload("tool.args", tool_name, arguments)

            if tool_name not in self.supported_tools:
                self.tracer.event("tool.error", "Tool is not supported.", name=tool_name)
                return self._result(
                    tool_call_id,
                    tool_name,
                    success=False,
                    error=f"Unsupported tool: {tool_name}",
                )

            # Local runs default to read-only mode so a prompt experiment cannot
            # accidentally create, complete, update, or delete real Todoist tasks.
            if tool_name in MUTATING_TOOL_NAMES and not self.allow_mutations:
                self.tracer.event(
                    "tool.blocked",
                    "Mutation blocked by ALLOW_MUTATIONS = False.",
                    name=tool_name,
                )
                return self._result(
                    tool_call_id,
                    tool_name,
                    success=False,
                    error=(
                        f"Mutation blocked for {tool_name}. Set ALLOW_MUTATIONS = True "
                        "in agents/jarvis.py to allow real Todoist changes."
                    ),
                    mutation_blocked=True,
                )

            content = self.supported_tools[tool_name](arguments)
            self.tracer.event("tool.done", "Tool call completed.", name=tool_name)
            self.tracer.payload("tool.result", tool_name, content)
            return self._result(tool_call_id, tool_name, success=True, content=content)
        except Exception as error:
            self.tracer.event("tool.error", "Tool call failed.", name=tool_name, error=str(error))
            return self._result(
                tool_call_id,
                tool_name,
                success=False,
                error=str(error),
            )

    @staticmethod
    def _parse_arguments(arguments_json: Any) -> Dict[str, Any]:
        if isinstance(arguments_json, dict):
            return arguments_json
        if not arguments_json:
            return {}
        parsed = json.loads(arguments_json)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must decode to a JSON object.")
        return parsed

    @staticmethod
    def _result(
        tool_call_id: str,
        tool_name: str,
        success: bool,
        content: Any = None,
        error: Optional[str] = None,
        mutation_blocked: bool = False,
    ) -> Dict[str, Any]:
        return {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "success": success,
            "content": content,
            "error": error,
            "mutation_blocked": mutation_blocked,
        }


def tool_result_to_message(result: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a dispatcher result into a raw chat tool message."""

    # The next agent turn sees this as the observation for its prior tool call.
    return {
        "role": "tool",
        "tool_call_id": result["tool_call_id"],
        "name": result["tool_name"],
        "content": json.dumps(result, default=str),
    }


def openai_tool_call_to_toolnode_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw OpenAI-compatible tool call into ToolNode's direct-call shape."""

    return {
        "name": tool_call_name(tool_call),
        "args": parse_tool_call_arguments(tool_call),
        "id": tool_call.get("id", "missing_tool_call_id"),
        "type": "tool_call",
    }


def toolnode_output_messages(output: Any) -> List[ToolMessage]:
    """Extract ToolMessages from ToolNode output across supported input modes."""

    if isinstance(output, dict):
        return output.get("messages", [])
    return output or []


def tool_message_to_result(tool_message: ToolMessage) -> Dict[str, Any]:
    """Convert a ToolNode ToolMessage into the existing Jarvis tool result envelope."""

    content = tool_message.content
    parsed_content: Any = content
    if isinstance(content, str):
        try:
            parsed_content = json.loads(content)
        except json.JSONDecodeError:
            parsed_content = content

    if isinstance(parsed_content, dict) and {
        "tool_call_id",
        "tool_name",
        "success",
        "content",
        "error",
        "mutation_blocked",
    }.issubset(parsed_content.keys()):
        return parsed_content

    failed = getattr(tool_message, "status", "success") == "error"
    return TodoistToolDispatcher._result(
        tool_message.tool_call_id,
        tool_message.name or "unknown",
        success=not failed,
        content=None if failed else parsed_content,
        error=str(content) if failed else None,
    )


def build_todoist_langchain_tools(tool_dispatcher: TodoistToolDispatcher) -> List[Any]:
    """Build LangChain tool wrappers that delegate to the existing dispatcher."""

    def dispatch(tool_call_id: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return tool_dispatcher.execute_tool(tool_call_id, tool_name, arguments)

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
        assignee_id: Optional[str] = None,
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
                "assignee_id": assignee_id,
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
        label: Optional[str] = None,
        filter: Optional[str] = None,
        lang: Optional[str] = None,
        ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """List active Todoist tasks with optional filters."""

        return dispatch(
            tool_call_id,
            "get_tasks",
            {
                "project_id": project_id,
                "section_id": section_id,
                "label": label,
                "filter": filter,
                "lang": lang,
                "ids": ids,
            },
        )

    @tool
    def update_todoist_task(
        task_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        content: Optional[str] = None,
        description: Optional[str] = None,
        labels: Optional[List[str]] = None,
        priority: Optional[int] = None,
        due_string: Optional[str] = None,
        due_date: Optional[str] = None,
        due_datetime: Optional[str] = None,
        assignee_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an existing Todoist task."""

        return dispatch(
            tool_call_id,
            "update_todoist_task",
            {
                "task_id": task_id,
                "content": content,
                "description": description,
                "labels": labels,
                "priority": priority,
                "due_string": due_string,
                "due_date": due_date,
                "due_datetime": due_datetime,
                "assignee_id": assignee_id,
            },
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
        update_todoist_task,
        complete_task,
        delete_todoist_task,
        get_completed_todoist_tasks_by_completion_date,
    ]


def execute_tool_calls_with_toolnode(
    tool_calls: List[Dict[str, Any]],
    tool_node: ToolNode,
    tool_dispatcher: TodoistToolDispatcher,
) -> List[Dict[str, Any]]:
    """Execute supported calls through ToolNode and return ordered Jarvis results."""

    toolnode_calls: List[Dict[str, Any]] = []
    results_by_id: Dict[str, Dict[str, Any]] = {}

    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        tool_name = tool_call_name(tool_call)

        try:
            toolnode_call = openai_tool_call_to_toolnode_call(tool_call)
        except Exception:
            results_by_id[tool_call_id] = tool_dispatcher.execute_tool_call(tool_call)
            continue

        if tool_name not in tool_dispatcher.supported_tools:
            results_by_id[tool_call_id] = tool_dispatcher.execute_tool(
                tool_call_id,
                tool_name,
                toolnode_call["args"],
            )
            continue

        toolnode_calls.append(toolnode_call)

    if toolnode_calls:
        output = tool_node.invoke(toolnode_calls)
        for tool_message in toolnode_output_messages(output):
            result = tool_message_to_result(tool_message)
            results_by_id[result["tool_call_id"]] = result

    ordered_results = []
    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "missing_tool_call_id")
        if tool_call_id in results_by_id:
            ordered_results.append(results_by_id[tool_call_id])
    return ordered_results


def tool_call_name(tool_call: Dict[str, Any]) -> str:
    """Return the function name for an OpenAI-compatible tool call."""

    return tool_call.get("function", {}).get("name", "unknown")


def is_ask_user_tool_call(tool_call: Dict[str, Any]) -> bool:
    """Return whether a tool call is the HITL clarification pseudo-tool."""

    return tool_call_name(tool_call) == ASK_USER_TOOL_NAME


def parse_tool_call_arguments(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a tool call's JSON arguments into a dictionary."""

    return TodoistToolDispatcher._parse_arguments(
        tool_call.get("function", {}).get("arguments", "{}")
    )


__all__ = [
    "TodoistToolDispatcher",
    "build_todoist_langchain_tools",
    "execute_tool_calls_with_toolnode",
    "is_ask_user_tool_call",
    "openai_tool_call_to_toolnode_call",
    "parse_tool_call_arguments",
    "tool_call_name",
    "tool_message_to_result",
    "tool_result_to_message",
    "toolnode_output_messages",
]
