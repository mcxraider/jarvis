"""Shared base for classified, source-tagged external-API failures.

Every tool domain (Todoist today; Google Calendar and others later) raises a
subclass of :class:`ClassifiedApiError` when an external API fails in a way the
runtime can reason about. The dispatcher catches this *base* type, so a new
domain's errors flow through the same tool-result envelope, tracing, and
idempotency-abandon path without the domain-neutral dispatcher importing any
concrete domain package.

A subclass carries:
- ``kind``: a coarse, provider-neutral class — one of "auth", "not-found",
  "rate-limit", "transient", "validation" (domains may add their own).
- ``message``: a user-safe string surfaced to the model as the tool error.
- ``retryable``: whether the caller's retry loop should attempt again.
- ``to_classifier_payload()``: the structured dict attached to the tool result
  under ``classified_error`` so downstream policy can branch on it.
"""

from typing import Any, Dict, Optional


class ClassifiedApiError(Exception):
    """Base class for structured external-API failures routed through tool results.

    Subclasses (typically ``@dataclass``) redeclare these as real fields. The
    class-level defaults here define the interface the dispatcher relies on and
    provide a sane fallback ``to_classifier_payload`` for simple subclasses.
    """

    source: str = "unclassified"
    kind: str = "transient"
    message: str = ""
    retryable: bool = False
    status_code: Optional[int] = None
    attempts: int = 1

    def __str__(self) -> str:
        return self.message or self.__class__.__name__

    def to_classifier_payload(self) -> Dict[str, Any]:
        """Structured, log-safe description of the failure for the tool result."""

        payload: Dict[str, Any] = {
            "source": self.source,
            "kind": self.kind,
            "retryable": self.retryable,
            "attempts": self.attempts,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


__all__ = ["ClassifiedApiError"]
