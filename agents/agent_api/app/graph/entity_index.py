"""Index of entity IDs surfaced by prior successful reads in a thread.

Backs the prior-read ID validation guard: a mutation may only target an entity ID
that an earlier read tool actually returned. The index is built from
``state["tool_results"]`` (the structured Jarvis result envelopes), so no JSON
re-parsing of chat messages is needed.

v1 tracks Todoist tasks only. Extending to other domains is a matter of registering
a per-``entity_type`` extractor here and adding ``EntityRef``s in ``tools/metadata.py``.
"""

from typing import Any, Dict, List, Set, Tuple

from agents.agent_api.app.graph.extractors import extract_task_items
from agents.agent_api.app.tools.base import parse_tool_call_arguments, tool_call_name
from agents.agent_api.app.tools.metadata import entity_requirements


class SeenEntityIndex:
    """IDs returned by prior successful reads. O(1) membership after build."""

    def __init__(self, tool_results: List[Dict[str, Any]]):
        self._seen: Set[Tuple[str, str]] = set()  # (entity_type, id)
        for result in tool_results or []:
            if not result.get("success"):
                continue
            for task in extract_task_items(result.get("content")):
                task_id = task.get("id")
                if task_id:
                    self._seen.add(("task", str(task_id)))

    def has(self, entity_type: str, entity_id: str) -> bool:
        """Whether ``entity_id`` of ``entity_type`` was surfaced by a prior read."""
        return (entity_type, str(entity_id)) in self._seen

    def violations(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Unseen required entity IDs across ``tool_calls``.

        Returns one dict per violation:
        ``{tool_call_id, tool_name, arg, entity_type, value}``. A requirement is only
        checked when its argument is present and non-empty (so optional refs like
        ``add_comment.task_id`` are skipped when absent).
        """
        found: List[Dict[str, Any]] = []
        for call in tool_calls:
            name = tool_call_name(call)
            refs = entity_requirements(name)
            if not refs:
                continue
            args = parse_tool_call_arguments(call)
            for ref in refs:
                value = args.get(ref.arg)
                if value in (None, ""):
                    continue
                if not self.has(ref.entity_type, str(value)):
                    found.append(
                        {
                            "tool_call_id": call.get("id", "missing_tool_call_id"),
                            "tool_name": name,
                            "arg": ref.arg,
                            "entity_type": ref.entity_type,
                            "value": str(value),
                        }
                    )
        return found


__all__ = ["SeenEntityIndex"]
