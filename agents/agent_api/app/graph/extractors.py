"""Shared extraction utilities for tool result content."""

from typing import Any, Dict, List, Optional


def extract_list_from_content(content: Any) -> Optional[List[Any]]:
    """Extract a list from tool result content. Shared by edge routing and summarize node."""
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        for key in ("results", "tasks", "items"):
            if key in content and isinstance(content[key], list):
                return content[key]
    return None


def extract_task_items(content: Any) -> List[Dict[str, Any]]:
    """Return Todoist task dicts from any read-result shape OR a result envelope.

    Handles: a bare list; ``{"results": [...]}`` / ``{"items": [...]}`` /
    ``{"tasks": [...]}``; a single ``{"id": ...}`` task; and a full tool-result
    envelope (recurses into its ``"content"``). Shared by the seen-entity index
    and the prepare_confirm task-content enrichment.
    """
    # Unwrap Jarvis tool-result envelopes only. Envelopes are identified by
    # "tool_call_id" — a key task dicts never carry — so a task whose "content"
    # field happens to be a dict or list is never incorrectly recursed into.
    if (
        isinstance(content, dict)
        and "tool_call_id" in content
        and isinstance(content.get("content"), (list, dict))
    ):
        inner = extract_task_items(content["content"])
        if inner:
            return inner

    items = extract_list_from_content(content)
    if items is not None:
        return [item for item in items if isinstance(item, dict)]

    if isinstance(content, dict) and content.get("id"):
        return [content]

    return []


def extract_event_items(content: Any) -> List[Dict[str, Any]]:
    """Return Google Calendar event dicts from any read-result shape OR envelope.

    Calendar counterpart to :func:`extract_task_items`. Handles: a bare list; a
    ``{"events": [...]}`` payload (from ``list_calendar_events``); a single
    normalized event dict (from ``get_calendar_event``, identified by
    ``event_id``); and a full tool-result envelope (recurses into its
    ``"content"``). Shared by the prepare_confirm event-summary enrichment.
    """
    # Unwrap Jarvis tool-result envelopes only. Envelopes are identified by
    # "tool_call_id" — a key event dicts never carry — so an event whose value
    # happens to be a dict or list is never incorrectly recursed into.
    if (
        isinstance(content, dict)
        and "tool_call_id" in content
        and isinstance(content.get("content"), (list, dict))
    ):
        inner = extract_event_items(content["content"])
        if inner:
            return inner

    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]

    if isinstance(content, dict):
        events = content.get("events")
        if isinstance(events, list):
            return [item for item in events if isinstance(item, dict)]
        if content.get("event_id"):
            return [content]

    return []


__all__ = ["extract_event_items", "extract_list_from_content", "extract_task_items"]
