"""Render tool_results as a dependency/parallelism tree."""

from itertools import groupby
from typing import Dict, List


def _status_icon(result: Dict) -> str:
    if result.get("mutation_blocked"):
        return "⛔"
    return "✅" if result.get("success") else "❌"


def _format_entry(result: Dict) -> str:
    icon = _status_icon(result)
    service = result.get("service") or ""
    name = result.get("tool_name", "unknown")
    if service:
        return f"{name} [{service} {icon}]"
    return f"{name} [{icon}]"


def render_tool_tree(tool_results: List[Dict]) -> str:
    """Render tool results grouped by batch with tree-drawing characters.

    Within a batch all calls ran in parallel. Sequential batches mean
    the LLM needed earlier results before issuing the next calls.
    Falls back to flat numbered rendering if no batch_index metadata present.
    """
    if not tool_results:
        return "None"

    has_batches = any(r.get("batch_index") is not None for r in tool_results)
    if not has_batches:
        return _render_flat(tool_results)

    lines: List[str] = []
    sorted_results = sorted(tool_results, key=lambda r: r.get("batch_index", 0))
    batches = groupby(sorted_results, key=lambda r: r.get("batch_index", 0))

    for batch_idx, (_, batch_items) in enumerate(batches):
        items = list(batch_items)
        if batch_idx > 0:
            lines.append("│")

        for i, result in enumerate(items):
            entry = _format_entry(result)

            if len(items) == 1:
                prefix = "─"
            elif i == 0:
                prefix = "┬"
            elif i == len(items) - 1:
                prefix = "└"
            else:
                prefix = "├"

            lines.append(f"{prefix} {entry}")

            if result.get("error"):
                indent = "│ " if i < len(items) - 1 else "  "
                lines.append(f"{indent} ↳ {result['error']}")

    return "\n".join(lines)


def _render_flat(tool_results: List[Dict]) -> str:
    """Legacy flat rendering when batch metadata is absent."""
    lines = []
    for i, item in enumerate(tool_results, start=1):
        status = "ok" if item.get("success") else "error"
        blocked = " blocked" if item.get("mutation_blocked") else ""
        lines.append(f"{i}. {item.get('tool_name')} [{status}{blocked}]")
        if item.get("error"):
            lines.append(f"   {item['error']}")
    return "\n".join(lines)
