"""LangGraph state schema and interrupt response helpers."""

import operator
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict


class JarvisState(TypedDict, total=False):
    """Shared state LangGraph passes between nodes on each loop."""

    # Raw chat messages are the agent's memory and include tool call/result turns.
    messages: List[Dict[str, Any]]
    user_prompt: str
    reply_context: Optional[Dict[str, str]]
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
    run_log_path: str
    next: str
    runtime_context: Dict[str, Any]
    selected_tool_names: List[str]
    active_domains: List[str]
    router_outcome: Optional[Literal["routed", "conversation", "unsupported_provider", "ambiguous"]]

    # Confirm gate fields — freeze risky actions for human approval.
    held_calls: Optional[List[Dict[str, Any]]]
    pending_interrupt: Optional[Literal["clarify", "confirm"]]
    confirm_decision: Optional[Literal["approve", "decline"]]
    consumed_call_ids: Annotated[List[str], operator.add]


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
        interrupt_type = interrupt_payload.get("type", "clarify")
        enriched["pending_interrupt"] = interrupt_type
        enriched["pending_clarification"] = interrupt_payload
        enriched["next"] = "confirm" if interrupt_type == "confirm" else "hitl"
    return enriched


__all__ = ["JarvisState", "enrich_interrupt_status"]
