"""Tool display and risk metadata — single source of truth for the confirm gate.

ToolSpec (in base.py) owns runtime concerns: schema, handler, mutating flag.
This module layers on *presentation and risk classification* metadata consumed
by graph nodes (confirm, prepare_confirm) and the tracing/progress pipeline.

Adding a tool domain means adding entries to _REGISTRY here — no edits to
graph nodes or risk classification logic.
"""

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class EntityRef:
    """One entity-ID argument a tool needs verified against prior reads."""

    arg: str                 # the tool argument carrying the id, e.g. "task_id"
    entity_type: str         # the entity it references, e.g. "task"
    required: bool = True    # documents whether the arg is mandatory for the tool


@dataclass(frozen=True)
class ToolDisplayMeta:
    """Display and risk metadata for one tool."""

    verb: str
    label: str
    irreversible: bool = False
    always_risky: bool = False
    needs_task_context: bool = False
    highlight_arg: Optional[str] = None
    render_fn: Optional[Callable[[dict], str]] = None


def _render_task_with_context(held_call: dict) -> str:
    meta = _REGISTRY[held_call["tool_name"]]
    context = held_call.get("context", {})
    task_content = context.get("task_content")
    if task_content:
        return f'{meta.label} "{task_content}".'
    task_id = held_call.get("args", {}).get("task_id", "unknown")
    return f"{meta.label} (id={task_id})."


def _render_bulk_add(held_call: dict) -> str:
    args = held_call.get("args", {})
    content = args.get("content", "")
    count = args.get("count", "?")
    meta = _REGISTRY["bulk_add_todoist_tasks"]
    if content:
        return f'{meta.label}: {count}× "{content}".'
    return f"{meta.label}: {count} tasks."


def _render_update(held_call: dict) -> str:
    args = held_call.get("args", {})
    task_id = args.get("task_id", "unknown")
    changed = [k for k in args if k != "task_id"]
    meta = _REGISTRY["update_todoist_task"]
    fields = ", ".join(changed[:3])
    context = held_call.get("context", {})
    task_content = context.get("task_content")
    target = f'"{task_content}"' if task_content else f"(id={task_id})"
    if fields:
        return f"{meta.label} {target}: {fields}."
    return f"{meta.label} {target}."


_REGISTRY: Dict[str, ToolDisplayMeta] = {
    "delete_todoist_task": ToolDisplayMeta(
        verb="deleting",
        label="Delete task",
        irreversible=True,
        always_risky=True,
        needs_task_context=True,
        render_fn=_render_task_with_context,
    ),
    "bulk_add_todoist_tasks": ToolDisplayMeta(
        verb="adding",
        label="Bulk-add tasks",
        always_risky=True,
        render_fn=_render_bulk_add,
    ),
    "add_todoist_task": ToolDisplayMeta(
        verb="adding",
        label="Add task",
        highlight_arg="content",
    ),
    "update_todoist_task": ToolDisplayMeta(
        verb="updating",
        label="Update task",
        needs_task_context=True,
        render_fn=_render_update,
    ),
    "complete_task": ToolDisplayMeta(
        verb="completing",
        label="Complete task",
        needs_task_context=True,
        render_fn=_render_task_with_context,
    ),
}

DEFAULT_META = ToolDisplayMeta(verb="modifying", label="Modify item")

_IRREVERSIBLE_TOOLS = frozenset(name for name, m in _REGISTRY.items() if m.irreversible)
_ALWAYS_RISKY_TOOLS = frozenset(name for name, m in _REGISTRY.items() if m.always_risky)
_NEEDS_TASK_CONTEXT = frozenset(
    name for name, m in _REGISTRY.items() if m.needs_task_context
)


def get_meta(tool_name: str) -> ToolDisplayMeta:
    """Look up display metadata for a tool. Returns DEFAULT_META for unknown tools."""
    return _REGISTRY.get(tool_name, DEFAULT_META)


def get_verb(tool_name: str) -> str:
    """Present-participle verb for a tool (e.g. 'deleting', 'adding')."""
    return get_meta(tool_name).verb


def irreversible_tools() -> frozenset:
    """Tools whose effects cannot be undone (drives suffix text in confirmations)."""
    return _IRREVERSIBLE_TOOLS


def always_risky_tools() -> frozenset:
    """Tools that always route to the confirm gate regardless of count."""
    return _ALWAYS_RISKY_TOOLS


def needs_task_context_tools() -> frozenset:
    """Tools that benefit from task-content enrichment before confirmation."""
    return _NEEDS_TASK_CONTEXT


# Prior-read ID validation: entity-ID args that must have been surfaced by a prior
# read before a mutation runs. Tools absent from this map skip validation (fail-open).
# v1 validates `task_id` only; project/section/parent ids have no read tool to emit them.
_ENTITY_REQUIREMENTS: Dict[str, Tuple[EntityRef, ...]] = {
    "complete_task": (EntityRef("task_id", "task"),),
    "uncomplete_task": (EntityRef("task_id", "task"),),
    "update_todoist_task": (EntityRef("task_id", "task"),),
    "delete_todoist_task": (EntityRef("task_id", "task"),),
    # add_comment targets a task OR a project, so task_id is only validated when present.
    "add_comment": (EntityRef("task_id", "task", required=False),),
}


def entity_requirements(tool_name: str) -> Tuple[EntityRef, ...]:
    """Entity-ID args a tool needs verified against prior reads. ``()`` = no validation."""
    return _ENTITY_REQUIREMENTS.get(tool_name, ())


__all__ = [
    "DEFAULT_META",
    "EntityRef",
    "ToolDisplayMeta",
    "always_risky_tools",
    "entity_requirements",
    "get_meta",
    "get_verb",
    "irreversible_tools",
    "needs_task_context_tools",
]
