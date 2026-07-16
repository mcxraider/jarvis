"""Prepare-confirm node — freezes the risky call and defers siblings.

This thin node runs between the agent router's "confirm" edge and the confirm
node. It partitions tool calls, builds the held_call artifact, sets
pending_interrupt, and creates synthetic deferred messages for sibling calls.
"""

import json
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig

from agents.agent_api.app.graph.canonicalize import build_held_call
from agents.agent_api.app.graph.extractors import extract_event_items, extract_task_items
from agents.agent_api.app.graph.nodes.hitl import deferred_tool_message
from agents.agent_api.app.graph.risk import partition_tool_calls
from agents.agent_api.app.graph.run_deps import RunDeps, deps_from_config
from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.metadata import (
    needs_event_context_tools,
    needs_task_context_tools,
)
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


def _find_task_content(messages: List[Dict[str, Any]], task_id: str) -> Optional[str]:
    """Search prior tool results for the content/name of a Todoist task by id."""
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content_str = msg.get("content", "")
        if task_id not in content_str:
            continue
        try:
            data = json.loads(content_str)
            for task in extract_task_items(data):
                if task.get("id") == task_id:
                    return task.get("content")
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _find_event_summary(messages: List[Dict[str, Any]], event_id: str) -> Optional[str]:
    """Search prior tool results for the summary/title of a Calendar event by id."""
    if not event_id:
        return None
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content_str = msg.get("content", "")
        if event_id not in content_str:
            continue
        try:
            data = json.loads(content_str)
            for event in extract_event_items(data):
                if event.get("event_id") == event_id:
                    return event.get("summary")
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def create_prepare_confirm_node(tracer: Optional[TracePrinter] = None):
    """Create the node that freezes a risky action into state before confirmation."""

    _captured = RunDeps(tracer=tracer or NULL_TRACE)

    async def prepare_confirm_node(
        state: JarvisState,
        config: RunnableConfig | None = None,
    ) -> JarvisState:
        deps = deps_from_config(config)
        tracer = (
            deps.tracer
            if deps is not None and deps.tracer is not None
            else _captured.tracer
        )
        messages = list(state.get("messages", []))
        latest_message = messages[-1] if messages else {}
        tool_calls = latest_message.get("tool_calls") or []

        risky, safe = partition_tool_calls(tool_calls)

        if not risky:
            error = "prepare_confirm reached without any risky tool calls."
            tracer.event("graph.prepare_confirm", "No risky calls found.", error=error)
            return {"error": error, "final_response": error, "next": "end"}

        full_index = {id(tc): i for i, tc in enumerate(tool_calls)}
        held_calls = [
            build_held_call(tc, state.get("thread_id", ""), state.get("turn_count", 0), call_index=full_index[id(tc)])
            for tc in risky
        ]

        for held in held_calls:
            if held["tool_name"] in needs_task_context_tools():
                task_id = held["args"].get("task_id", "")
                task_content = _find_task_content(messages, task_id)
                if task_content:
                    held["context"] = {"task_content": task_content}
            elif held["tool_name"] in needs_event_context_tools():
                event_id = held["args"].get("event_id", "")
                event_summary = _find_event_summary(messages, event_id)
                if event_summary:
                    held["context"] = {"event_summary": event_summary}

        deferred_messages = [
            deferred_tool_message(tc, "Deferred pending batch confirmation.")
            for tc in safe
        ]

        tracer.event(
            "graph.prepare_confirm",
            "Froze risky actions into held_calls.",
            held_call_count=len(held_calls),
            tool_names=list({h["tool_name"] for h in held_calls}),
            deferred_count=len(safe),
        )
        tracer.progress({"phase": "preparing_change", "action": "started", "intent": "mutation"})

        return {
            "held_calls": held_calls,
            "pending_interrupt": "confirm",
            "messages": messages + deferred_messages,
        }

    return prepare_confirm_node


__all__ = ["create_prepare_confirm_node"]
