"""Shared extraction utilities for tool result content."""

from typing import Any, List, Optional


def extract_list_from_content(content: Any) -> Optional[List[Any]]:
    """Extract a list from tool result content. Shared by edge routing and summarize node."""
    if isinstance(content, list):
        return content
    if isinstance(content, dict):
        for key in ("results", "tasks", "items"):
            if key in content and isinstance(content[key], list):
                return content[key]
    return None


__all__ = ["extract_list_from_content"]
