"""Stable canonicalization and hashing for held tool calls.

Provides deterministic serialization so the hash of a frozen action is
reproducible and can be verified at execution time (hash-binding guard).
"""

import hashlib
import json
import uuid
from typing import Any, Dict

from agents.agent_api.app.tools.base import parse_tool_call_arguments, tool_call_name


def canonicalize(args: Dict[str, Any]) -> bytes:
    """Stable JSON serialization: sorted keys, no whitespace."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode()


def build_held_call(tool_call: Dict[str, Any], thread_id: str, turn_count: int) -> Dict[str, Any]:
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
        "idempotency_key": hashlib.sha256(
            canonical + thread_id.encode() + str(turn_count).encode()
        ).hexdigest(),
    }


def verify_hash(held_call: Dict[str, Any]) -> bool:
    """Check that a held_call's args still match its stored hash."""
    canonical = canonicalize(held_call["args"])
    return hashlib.sha256(canonical).hexdigest() == held_call["hash"]


__all__ = ["build_held_call", "canonicalize", "verify_hash"]
