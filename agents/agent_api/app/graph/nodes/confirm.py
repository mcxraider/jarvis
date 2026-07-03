"""Confirm node — interrupts the graph for human approval of risky actions.

This node does NOT execute anything. It shows the frozen held_calls to the user,
records their approve/decline decision, and lets the downstream router dispatch
to the executor (on approve) or END (on decline).
"""

from typing import List, Optional

from langgraph.types import interrupt

from agents.agent_api.app.graph.state import JarvisState
from agents.agent_api.app.tools.metadata import get_meta, get_service, get_verb, irreversible_tools
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter

APPROVE_TOKENS = frozenset({"approve", "yes", "confirm", "ok", "y"})


def parse_decision(reply: str) -> str:
    """Normalize a human reply to 'approve' or 'decline'."""
    normalized = str(reply).strip().lower()
    if normalized in APPROVE_TOKENS:
        return "approve"
    return "decline"


def _with_service(text: str, service: str) -> str:
    """Append a ' (service)' label to an action line, before any trailing period."""
    if not service:
        return text
    if text.endswith("."):
        return f"{text[:-1]} ({service})."
    return f"{text} ({service})"


def render_action_summary(held_call: dict) -> str:
    """Produce a human-readable summary of a single frozen action.

    The tool's service (todoist / google calendar) is appended as a ' (service)'
    suffix so the user sees which system the action targets before confirming.
    """
    tool_name = held_call.get("tool_name", "unknown")
    args = held_call.get("args", {})
    meta = get_meta(tool_name)

    if meta.render_fn:
        base_summary = meta.render_fn(held_call)
    elif meta.highlight_arg and args.get(meta.highlight_arg, ""):
        base_summary = f'{meta.label} "{args[meta.highlight_arg]}".'
    else:
        arg_summary = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
        base_summary = f"{tool_name}({arg_summary})"

    return _with_service(base_summary, get_service(tool_name))


def _batch_suffix(tool_names: set) -> str:
    """Context-sensitive caution suffix based on which tools are in the batch."""
    irrev = irreversible_tools()
    if tool_names and tool_names <= irrev:
        return "This action is irreversible."
    if tool_names & irrev:
        return "Some of these actions are irreversible."
    return "Please confirm to proceed."


def render_batch_summary(held_calls: List[dict]) -> str:
    """Produce a human-readable summary for a batch of frozen actions."""
    if len(held_calls) == 1:
        tool_name = held_calls[0].get("tool_name", "")
        suffix = _batch_suffix({tool_name})
        return f"{render_action_summary(held_calls[0])}\n{suffix}"

    tool_names = {h.get("tool_name", "") for h in held_calls}
    verbs = {get_verb(name) for name in tool_names}

    if len(verbs) == 1:
        header = f"I'm {next(iter(verbs))} {len(held_calls)} items:"
    else:
        header = f"I'm modifying {len(held_calls)} items ({', '.join(sorted(verbs))}):"

    lines = [header]
    for i, held in enumerate(held_calls, 1):
        lines.append(f"  {i}. {render_action_summary(held)}")
    lines.append(_batch_suffix(tool_names))
    return "\n".join(lines)


def create_confirm_node(tracer: Optional[TracePrinter] = None):
    """Create the graph node that pauses for user approval of risky actions."""

    tracer = tracer or NULL_TRACE

    def confirm_node(state: JarvisState) -> JarvisState:
        held_calls = state.get("held_calls") or []
        # Migration shim: support old singular held_call field
        if not held_calls and state.get("held_call"):
            held_calls = [state["held_call"]]

        if not held_calls:
            error = "Confirm node reached without held_calls in state."
            tracer.event("graph.confirm", "Missing held_calls.", error=error)
            return {
                "error": error,
                "final_response": error,
                "next": "end",
            }

        summary = render_batch_summary(held_calls)
        payload = {
            "type": "confirm",
            "held_call_ids": [h["id"] for h in held_calls],
            "summary": summary,
            "count": len(held_calls),
            "tool_names": list({h["tool_name"] for h in held_calls}),
            "services": sorted({get_service(h["tool_name"]) for h in held_calls} - {""}),
        }

        tracer.event(
            "graph.confirm",
            "Interrupting for user confirmation.",
            count=len(held_calls),
            tool_names=payload["tool_names"],
            summary=summary,
        )

        human_reply = interrupt(payload)
        decision = parse_decision(human_reply)

        tracer.event(
            "graph.confirm",
            "Resumed from user confirmation.",
            decision=decision,
            count=len(held_calls),
        )

        if decision == "decline":
            return {
                "pending_interrupt": None,
                "confirm_decision": decision,
                "final_response": "Action declined — no changes were made.",
            }

        return {
            "pending_interrupt": None,
            "confirm_decision": decision,
        }

    return confirm_node


__all__ = ["create_confirm_node", "parse_decision", "render_action_summary", "render_batch_summary"]
