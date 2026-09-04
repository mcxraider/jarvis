"""Domain-neutral tool definitions, the tool registry, and tool-call helpers.

A :class:`ToolSpec` is the single source of truth for one tool: the schema the
LLM sees, its synchronous and optional native-async handlers, and whether it
mutates external state. A :class:`ToolRegistry` aggregates specs across domains
so the graph core depends on this interface — never on a concrete domain package
such as Todoist.

Adding a tool domain is "write a module that returns ``list[ToolSpec]`` and
register it" — no edits to the graph nodes.
"""

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional


DispatchFn = Callable[[str, str, Dict[str, Any]], Dict[str, Any]]
AsyncToolHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    """Everything the runtime needs to know about one tool, in one place."""

    name: str
    openai_schema: Dict[str, Any]
    handler: Optional[Callable[[Dict[str, Any]], Any]] = None
    mutating: bool = False
    async_handler: Optional[AsyncToolHandler] = None


class ToolRegistry:
    """Aggregates tool specs for the graph."""

    def __init__(self) -> None:
        self._specs: List[ToolSpec] = []
        self._by_name: Dict[str, ToolSpec] = {}

    def register(self, specs: List[ToolSpec]) -> "ToolRegistry":
        """Add a domain's tools to the registry."""

        for spec in specs:
            if spec.name in self._by_name:
                raise ValueError(f"Duplicate tool registered: {spec.name}")
            self._specs.append(spec)
            self._by_name[spec.name] = spec
        return self

    @property
    def specs(self) -> List[ToolSpec]:
        return list(self._specs)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._by_name.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def openai_schemas(self) -> List[Dict[str, Any]]:
        """The tool list the LLM sees (registration order)."""

        return [spec.openai_schema for spec in self._specs]

    def mutating_names(self) -> set:
        """Names the mutation guard must block when mutations are disabled."""

        return {spec.name for spec in self._specs if spec.mutating}

    def handler_map(self) -> Dict[str, Callable[[Dict[str, Any]], Any]]:
        """Name -> handler for every tool that is actually executable."""

        return {spec.name: spec.handler for spec in self._specs if spec.handler is not None}

    def async_handler_map(self) -> Dict[str, AsyncToolHandler]:
        """Name -> native async handler where a domain provides one."""

        return {
            spec.name: spec.async_handler
            for spec in self._specs
            if spec.async_handler is not None
        }

def parse_arguments(arguments_json: Any) -> Dict[str, Any]:
    """Parse tool-call arguments (JSON string or dict) into a dict."""

    if isinstance(arguments_json, dict):
        return arguments_json
    if not arguments_json:
        return {}
    parsed = json.loads(arguments_json)
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must decode to a JSON object.")
    return parsed


def tool_call_name(tool_call: Dict[str, Any]) -> str:
    """Return the function name for an OpenAI-compatible tool call."""

    return tool_call.get("function", {}).get("name", "unknown")


def parse_tool_call_arguments(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a tool call's JSON arguments into a dictionary."""

    return parse_arguments(tool_call.get("function", {}).get("arguments", "{}"))


__all__ = [
    "AsyncToolHandler",
    "DispatchFn",
    "ToolSpec",
    "ToolRegistry",
    "parse_arguments",
    "parse_tool_call_arguments",
    "tool_call_name",
]
