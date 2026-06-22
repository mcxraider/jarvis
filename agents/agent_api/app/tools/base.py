"""Domain-neutral tool definitions, the tool registry, and tool-call helpers.

A :class:`ToolSpec` is the single source of truth for one tool: the schema the
LLM sees, the handler that executes it, whether it mutates external state, and an
optional per-domain LangChain builder for ``ToolNode`` execution. A
:class:`ToolRegistry` aggregates specs across domains so the graph core depends on
this interface — never on a concrete domain package such as Todoist.

Adding a tool domain is "write a module that returns ``list[ToolSpec]`` (plus an
optional LangChain builder) and register it" — no edits to the graph nodes.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


# A dispatch function executes one parsed tool call through the shared guard +
# result-envelope pipeline. Domains build their LangChain tools against this
# signature so every tool goes through the same mutation guard and tracing.
DispatchFn = Callable[[str, str, Dict[str, Any]], Dict[str, Any]]

# A LangChain builder turns a dispatch function into that domain's ToolNode tools.
LangChainToolBuilder = Callable[[DispatchFn], List[Any]]


@dataclass(frozen=True)
class ToolSpec:
    """Everything the runtime needs to know about one tool, in one place."""

    name: str
    openai_schema: Dict[str, Any]
    handler: Optional[Callable[[Dict[str, Any]], Any]] = None
    mutating: bool = False


class ToolRegistry:
    """Aggregates tool specs (and per-domain LangChain builders) for the graph."""

    def __init__(self) -> None:
        self._specs: List[ToolSpec] = []
        self._by_name: Dict[str, ToolSpec] = {}
        self._langchain_builders: List[LangChainToolBuilder] = []

    def register(
        self,
        specs: List[ToolSpec],
        langchain_builder: Optional[LangChainToolBuilder] = None,
    ) -> "ToolRegistry":
        """Add a domain's tools (and optional LangChain builder) to the registry."""

        for spec in specs:
            if spec.name in self._by_name:
                raise ValueError(f"Duplicate tool registered: {spec.name}")
            self._specs.append(spec)
            self._by_name[spec.name] = spec
        if langchain_builder is not None:
            self._langchain_builders.append(langchain_builder)
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

    def build_langchain_tools(self, dispatch: DispatchFn) -> List[Any]:
        """Build the ToolNode tools for every registered domain."""

        tools: List[Any] = []
        for builder in self._langchain_builders:
            tools.extend(builder(dispatch))
        return tools


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


def openai_schema_from_tool(langchain_tool: Any) -> Dict[str, Any]:
    """Derive an OpenAI/DeepSeek function schema from a LangChain tool.

    Convenience for *new* simple tools so they can supply only a ``@tool`` and skip
    hand-writing JSON Schema. Existing tools with carefully hand-authored schemas
    (patterns, ``dependentRequired``, null-clearing) should keep passing those
    explicitly to ``ToolSpec`` instead of relying on this.
    """

    from langchain_core.utils.function_calling import convert_to_openai_tool

    return convert_to_openai_tool(langchain_tool)


__all__ = [
    "DispatchFn",
    "LangChainToolBuilder",
    "ToolSpec",
    "ToolRegistry",
    "openai_schema_from_tool",
    "parse_arguments",
    "parse_tool_call_arguments",
    "tool_call_name",
]
