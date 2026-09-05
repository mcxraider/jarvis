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
    service: str = ""  # human-facing service name, e.g. "todoist", "google calendar"
    irreversible: bool = False
    always_risky: bool = False
    needs_task_context: bool = False
    needs_event_context: bool = False
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


def _render_calendar_delete(held_call: dict) -> str:
    args = held_call.get("args", {})
    event_id = args.get("event_id", "unknown")
    meta = _REGISTRY["delete_calendar_event"]
    summary = held_call.get("context", {}).get("event_summary")
    if summary:
        return f'{meta.label} "{summary}".'
    return f"{meta.label} (id={event_id})."


def _render_calendar_update(held_call: dict) -> str:
    args = held_call.get("args", {})
    event_id = args.get("event_id", "unknown")
    changed = [k for k in args if k not in ("event_id", "calendar_id")]
    meta = _REGISTRY["update_calendar_event"]
    fields = ", ".join(changed[:3])
    summary = held_call.get("context", {}).get("event_summary")
    target = f'"{summary}"' if summary else f"(id={event_id})"
    if fields:
        return f"{meta.label} {target}: {fields}."
    return f"{meta.label} {target}."


_REGISTRY: Dict[str, ToolDisplayMeta] = {
    "delete_todoist_task": ToolDisplayMeta(
        verb="deleting",
        label="Delete task",
        service="todoist",
        irreversible=True,
        always_risky=True,
        needs_task_context=True,
        render_fn=_render_task_with_context,
    ),
    "add_todoist_task": ToolDisplayMeta(
        verb="adding",
        label="Add task",
        service="todoist",
        highlight_arg="content",
    ),
    "create_project": ToolDisplayMeta(
        verb="adding",
        label="Add project",
        service="todoist",
        highlight_arg="name",
    ),
    "create_section": ToolDisplayMeta(
        verb="adding",
        label="Add section",
        service="todoist",
        highlight_arg="name",
    ),
    "update_todoist_task": ToolDisplayMeta(
        verb="updating",
        label="Update task",
        service="todoist",
        needs_task_context=True,
        render_fn=_render_update,
    ),
    "complete_task": ToolDisplayMeta(
        verb="completing",
        label="Complete task",
        service="todoist",
        needs_task_context=True,
        render_fn=_render_task_with_context,
    ),
    # Google Calendar mutations. delete is irreversible + always gated; create
    # and update fall through the shared "single mutation = low, bulk = risky"
    # path via risk.MUTATING_TOOLS.
    "delete_calendar_event": ToolDisplayMeta(
        verb="deleting",
        label="Delete event",
        service="google calendar",
        irreversible=True,
        always_risky=True,
        needs_event_context=True,
        render_fn=_render_calendar_delete,
    ),
    "create_calendar_event": ToolDisplayMeta(
        verb="adding",
        label="Add event",
        service="google calendar",
        highlight_arg="summary",
    ),
    "update_calendar_event": ToolDisplayMeta(
        verb="updating",
        label="Update event",
        service="google calendar",
        needs_event_context=True,
        render_fn=_render_calendar_update,
    ),
}

DEFAULT_META = ToolDisplayMeta(verb="modifying", label="Modify item")

_IRREVERSIBLE_TOOLS = frozenset(name for name, m in _REGISTRY.items() if m.irreversible)
_ALWAYS_RISKY_TOOLS = frozenset(name for name, m in _REGISTRY.items() if m.always_risky)
_NEEDS_TASK_CONTEXT = frozenset(
    name for name, m in _REGISTRY.items() if m.needs_task_context
)
_NEEDS_EVENT_CONTEXT = frozenset(
    name for name, m in _REGISTRY.items() if m.needs_event_context
)


def get_meta(tool_name: str) -> ToolDisplayMeta:
    """Look up display metadata for a tool. Returns DEFAULT_META for unknown tools."""
    return _REGISTRY.get(tool_name, DEFAULT_META)


def get_verb(tool_name: str) -> str:
    """Present-participle verb for a tool (e.g. 'deleting', 'adding')."""
    return get_meta(tool_name).verb


def get_service(tool_name: str) -> str:
    """Human-facing service label for a tool ('todoist', 'google calendar').

    Returns '' for tools with no declared service (e.g. DEFAULT_META), so the
    confirm gate can skip the '(service)' suffix rather than render '(unknown)'.
    """
    return get_meta(tool_name).service


def irreversible_tools() -> frozenset:
    """Tools whose effects cannot be undone (drives suffix text in confirmations)."""
    return _IRREVERSIBLE_TOOLS


def always_risky_tools() -> frozenset:
    """Tools that always route to the confirm gate regardless of count."""
    return _ALWAYS_RISKY_TOOLS


def needs_task_context_tools() -> frozenset:
    """Tools that benefit from task-content enrichment before confirmation."""
    return _NEEDS_TASK_CONTEXT


def needs_event_context_tools() -> frozenset:
    """Tools that benefit from calendar event-summary enrichment before confirmation."""
    return _NEEDS_EVENT_CONTEXT


# Prior-read ID validation: entity-ID args that must have been surfaced by a prior
# read before a mutation runs. Tools absent from this map skip validation (fail-open).
# v1 validates `task_id` only. `get_projects` now surfaces project ids, but project_id
# grounding is enforced via the prompt rather than structurally, so add_todoist_task can
# still target the Inbox or a known id without a prior read (see orchestrator prompt).
_ENTITY_REQUIREMENTS: Dict[str, Tuple[EntityRef, ...]] = {
    "complete_task": (EntityRef("task_id", "task"),),
    "uncomplete_task": (EntityRef("task_id", "task"),),
    "update_todoist_task": (EntityRef("task_id", "task"),),
    "delete_todoist_task": (EntityRef("task_id", "task"),),
    # add_comment targets a task OR a project, so task_id is only validated when present.
    "add_comment": (EntityRef("task_id", "task", required=False),),
    "update_calendar_event": (EntityRef("event_id", "event"),),
    "delete_calendar_event": (EntityRef("event_id", "event"),),
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
    "get_service",
    "get_verb",
    "irreversible_tools",
    "needs_event_context_tools",
    "needs_task_context_tools",
]
