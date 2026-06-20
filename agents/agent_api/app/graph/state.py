"""LangGraph state schema and interrupt response helpers."""

from typing import Any, Dict, List, TypedDict


class JarvisState(TypedDict, total=False):
    """Shared state LangGraph passes between nodes on each loop."""

    # Raw chat messages are the agent's memory and include tool call/result turns.
    messages: List[Dict[str, Any]]
    user_prompt: str
    user_id: str
    request_source: str
    turn_count: int
    tool_results: List[Dict[str, Any]]
    pending_clarification: Dict[str, Any]
    clarification_history: List[Dict[str, Any]]
    thread_id: str
    interrupted: bool
    interrupt_payload: Dict[str, Any]
    final_response: str
    error: str
    next: str


def _interrupt_value(interrupt_item: Any) -> Dict[str, Any]:
    value = getattr(interrupt_item, "value", interrupt_item)
    return value if isinstance(value, dict) else {"value": value}


def enrich_interrupt_status(result: JarvisState, thread_id: str) -> JarvisState:
    """Add runner-friendly interrupt fields to a LangGraph invocation result."""

    enriched = dict(result)
    interrupts = enriched.get("__interrupt__") or []
    interrupt_payload = _interrupt_value(interrupts[0]) if interrupts else {}
    enriched["thread_id"] = thread_id
    enriched["interrupted"] = bool(interrupts)
    enriched["interrupt_payload"] = interrupt_payload
    if interrupts:
        enriched["pending_clarification"] = interrupt_payload
        enriched["next"] = "hitl"
    return enriched


__all__ = ["JarvisState", "enrich_interrupt_status"]
