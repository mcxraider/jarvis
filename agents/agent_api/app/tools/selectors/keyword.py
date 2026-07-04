"""Keyword-based tool selector.

Matches user queries against a keyword routing table to narrow the registered
tool set (Todoist, plus Google Calendar when connected) to typically 1-3 tools.
Falls back to all tools when no keyword matches — so the selector can only
improve routing, never degrade it. Routes may name tools from a domain that is
not registered this run; those names are dropped when schemas are selected.
"""

import logging
from typing import Any, Dict, List, Set

from agents.agent_api.app.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

# Keyword → tool name(s) mapping.
# Multi-word phrases must appear before their single-word substrings in
# matching (handled by longest-first scan in the selector).
KEYWORD_ROUTES: Dict[str, List[str]] = {
    # Create
    "add": ["add_todoist_task"],
    "create": ["add_todoist_task"],
    "remind": ["add_todoist_task"],
    # Read
    "show": ["get_tasks"],
    "list": ["get_tasks"],
    "what tasks": ["get_tasks"],
    "due": ["get_tasks"],
    "overdue": ["get_tasks"],
    "filter": ["get_tasks_by_filter"],
    "what did i complete": ["get_completed_todoist_tasks_by_completion_date"],
    "completed": ["get_completed_todoist_tasks_by_completion_date"],
    # Update
    "reschedule": ["get_tasks", "update_todoist_task"],
    "move": ["get_tasks", "update_todoist_task"],
    "priority": ["get_tasks", "update_todoist_task"],
    "edit": ["get_tasks", "update_todoist_task"],
    "change": ["get_tasks", "update_todoist_task"],
    # Complete
    "mark": ["get_tasks", "complete_task"],
    "done": ["get_tasks", "complete_task"],
    "complete": ["get_tasks", "complete_task"],
    # Uncomplete
    "reopen": ["get_tasks", "uncomplete_task"],
    "uncomplete": ["get_tasks", "uncomplete_task"],
    # Delete
    "delete": ["get_tasks", "delete_todoist_task"],
    "remove": ["get_tasks", "delete_todoist_task"],
    # Comments
    "comment": ["get_comments", "add_comment"],
    # Labels
    "label": ["get_labels"],
    # Google Calendar. These names are silently dropped when the calendar domain
    # is unregistered (select_schemas filters to registry membership), so the
    # routes are harmless on a Todoist-only run. Longest-first scan lets the
    # multi-word phrases win over their single-word substrings.
    "cancel meeting": ["list_calendar_events", "delete_calendar_event"],
    "cancel event": ["list_calendar_events", "delete_calendar_event"],
    "reschedule meeting": ["list_calendar_events", "update_calendar_event"],
    "move meeting": ["list_calendar_events", "update_calendar_event"],
    "calendar": ["list_calendar_events", "get_freebusy"],
    "schedule": ["list_calendar_events", "create_calendar_event", "get_freebusy"],
    "meeting": ["list_calendar_events", "create_calendar_event", "get_freebusy"],
    "appointment": ["list_calendar_events", "create_calendar_event", "get_freebusy"],
    "event": ["list_calendar_events", "create_calendar_event"],
    "free": ["get_freebusy"],
    "busy": ["get_freebusy"],
    "available": ["get_freebusy"],
    "availability": ["get_freebusy"],
}

# Tools that must always be available regardless of selection.
ALWAYS_INCLUDE: Set[str] = {"ask_user"}

# Pre-sorted keywords by length (longest first) for greedy matching.
_SORTED_KEYWORDS: List[str] = sorted(KEYWORD_ROUTES.keys(), key=len, reverse=True)


class KeywordToolSelector:
    """Narrows the tool set by matching keywords in the user query.

    Selection logic:
    1. Lowercase the query, scan for keyword matches (longest first).
    2. Union all matched tool names.
    3. If allow_mutations=False, drop mutating tools.
    4. If nothing matched → fallback to all tools.
    5. Always include tools in ALWAYS_INCLUDE (e.g. ask_user).
    6. Return only schemas for the selected tools.
    """

    def __init__(self, *, allow_mutations: bool = True, **kwargs: Any) -> None:
        self._allow_mutations = allow_mutations

    def select_schemas(self, query: str, registry: ToolRegistry) -> List[Dict[str, Any]]:
        selected_names = self._match(query)
        fallback = not selected_names

        if fallback:
            selected_names = {spec.name for spec in registry.specs}
        else:
            if not self._allow_mutations:
                mutating = registry.mutating_names()
                selected_names -= mutating

        selected_names |= self._always_include_from(registry)

        schemas = [
            spec.openai_schema
            for spec in registry.specs
            if spec.name in selected_names
        ]

        logger.debug(
            "tool_select: fallback=%s selected=%s query_prefix=%.40s",
            fallback,
            sorted(selected_names - ALWAYS_INCLUDE),
            query,
        )
        return schemas

    def _match(self, query: str) -> Set[str]:
        """Return tool names matching keywords found in the query."""
        q = query.lower()
        matched: Set[str] = set()
        for keyword in _SORTED_KEYWORDS:
            if keyword in q:
                matched.update(KEYWORD_ROUTES[keyword])
        return matched

    def _always_include_from(self, registry: ToolRegistry) -> Set[str]:
        """Return the subset of ALWAYS_INCLUDE that exists in the registry."""
        return {name for name in ALWAYS_INCLUDE if name in registry}
