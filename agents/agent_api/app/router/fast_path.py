"""Conservative deterministic fast path for unambiguous low-complexity queries."""

from __future__ import annotations

import re
from typing import Optional

from agents.agent_api.app.router.prompt import (
    QueryComplexity,
    RouterDecision,
    RouterOutcome,
)
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot

_CONVERSATION_ONLY = re.compile(
    r"^(?:hi|hello|hey|thanks|thank\s+you|ok|okay|bye|good\s+morning|"
    r"good\s+night|what\s+can\s+you\s+do|who\s+are\s+you|how\s+are\s+you)"
    r"[.!?]*$",
    re.IGNORECASE,
)
_EXPLICIT_TODOIST = re.compile(r"\btodoist\b", re.IGNORECASE)
_EXPLICIT_GOOGLE_CALENDAR = re.compile(
    r"\b(?:google\s+calendar|google\s+cal|gcal|google_calendar)\b",
    re.IGNORECASE,
)
_TASK_ANCHOR = re.compile(
    r"\b(?:task|tasks|to-?do|todos|project|projects|deadline|deadlines|inbox|"
    r"priority|priorities|label|labels|filter|filters|section|sections|subtask|subtasks)\b",
    re.IGNORECASE,
)
_GENERIC_SCHEDULING = re.compile(
    r"\b(?:schedule|scheduled|meeting|meetings|appointment|appointments|free|busy|"
    r"availability|available|calendar|event|events)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_PROVIDER = re.compile(
    r"\b(?:notion|e-?mail|gmail|slack|google\s+docs?|gdocs)\b",
    re.IGNORECASE,
)
_COMPLEX_OR_MULTI_STEP = re.compile(
    r"\b(?:analy[sz]e|analysis|compare|prioriti[sz]e|optimi[sz]e|strategy|strategic|"
    r"plan|planning|recommend|across|multiple|bulk|every|all\s+my|then|after\s+that|"
    r"before\s+that|while|and\s+then)\b|[,;]",
    re.IGNORECASE,
)


def _decision(outcome: RouterOutcome, domains: list[str], reasoning: str) -> RouterDecision:
    return RouterDecision(
        outcome=outcome,
        domains=domains,
        uncertain=False,
        candidate_domains=[],
        complexity=QueryComplexity.LOW,
        reasoning=reasoning,
    )


def fast_path_classify(
    query: str,
    snapshot: RuntimeContextSnapshot,
) -> Optional[RouterDecision]:
    """Return a strict decision only when low complexity is genuinely certain."""

    stripped = " ".join(query.strip().split())
    if not stripped:
        return None
    if _CONVERSATION_ONLY.fullmatch(stripped):
        return _decision(RouterOutcome.CONVERSATION, [], "fast path greeting")
    if len(stripped.split()) > 16:
        return None
    if _UNSUPPORTED_PROVIDER.search(stripped) or _COMPLEX_OR_MULTI_STEP.search(stripped):
        return None

    active = snapshot.active_providers()
    explicit_todoist = bool(_EXPLICIT_TODOIST.search(stripped))
    explicit_calendar = bool(_EXPLICIT_GOOGLE_CALENDAR.search(stripped))
    task_request = bool(_TASK_ANCHOR.search(stripped))
    generic_scheduling = bool(_GENERIC_SCHEDULING.search(stripped))

    # Cross-domain and generic scheduling requests still need the prompt's live
    # preference rules and complexity classification.
    if explicit_todoist and explicit_calendar:
        return None
    if explicit_calendar and task_request:
        return None
    if explicit_todoist and generic_scheduling:
        return None

    if explicit_calendar and "google_calendar" in active:
        return _decision(
            RouterOutcome.ROUTED,
            ["google_calendar"],
            "fast path explicit calendar",
        )
    if explicit_todoist and "todoist" in active:
        return _decision(
            RouterOutcome.ROUTED,
            ["todoist"],
            "fast path explicit todoist",
        )
    if task_request and not generic_scheduling:
        task_provider = snapshot.preferences.routing.task_provider
        if task_provider in active:
            return _decision(
                RouterOutcome.ROUTED,
                [task_provider],
                "fast path simple task",
            )
    return None


__all__ = ["fast_path_classify"]
