"""Stable canonicalization and hashing for held tool calls.

Provides deterministic serialization so the hash of a frozen action is
reproducible and can be verified at execution time (hash-binding guard).
"""

import hashlib
import json
import uuid
from typing import Any, Dict, Iterable

from agents.agent_api.app.tools.base import parse_tool_call_arguments, tool_call_name


def canonicalize(args: Dict[str, Any]) -> bytes:
    """Stable JSON serialization: sorted keys, no whitespace."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()


def _hash_components(components: Iterable[bytes]) -> str:
    """Hash unambiguously framed byte components."""

    digest = hashlib.sha256()
    for component in components:
        digest.update(len(component).to_bytes(8, byteorder="big"))
        digest.update(component)
    return digest.hexdigest()


def build_request_idempotency_key(
    logical_route: str,
    source: str,
    user_id: str,
    request_id: str,
) -> str:
    """Build a retry key scoped to one caller and logical API operation."""

    return _hash_components(
        component.encode()
        for component in ("request:v1", logical_route, source, user_id, request_id)
    )


def build_operation_idempotency_key(
    tool_name: str,
    args: Dict[str, Any],
    thread_id: str,
    turn_count: int,
    call_index: int = 0,
) -> str:
    """Build a replay key for one mutating operation in a graph turn.

    ``call_index`` is the 0-based position of the tool call within the
    assistant message's tool_calls array — it distinguishes intentional
    parallel duplicates (same args, different positions) from retries.
    """

    return _hash_components(
        (
            b"operation:v2",
            tool_name.encode(),
            canonicalize(args),
            thread_id.encode(),
            str(turn_count).encode(),
            str(call_index).encode(),
        )
    )


def build_held_call(tool_call: Dict[str, Any], thread_id: str, turn_count: int, call_index: int = 0) -> Dict[str, Any]:
    """Freeze a risky tool call into a held_call artifact."""
    args = parse_tool_call_arguments(tool_call)
    canonical = canonicalize(args)
    call_hash = hashlib.sha256(canonical).hexdigest()
    return {
        "id": str(uuid.uuid4()),
        "tool_name": tool_call_name(tool_call),
        "args": json.loads(canonical),
        "hash": call_hash,
        "origin_tool_call_id": tool_call.get("id", "missing_tool_call_id"),
        "idempotency_key": build_operation_idempotency_key(
            tool_call_name(tool_call),
            args,
            thread_id,
            turn_count,
            call_index,
        ),
    }


def verify_hash(held_call: Dict[str, Any]) -> bool:
    """Check that a held_call's args still match its stored hash."""
    canonical = canonicalize(held_call["args"])
    return hashlib.sha256(canonical).hexdigest() == held_call["hash"]


__all__ = [
    "build_held_call",
    "build_operation_idempotency_key",
    "build_request_idempotency_key",
    "canonicalize",
    "verify_hash",
]
