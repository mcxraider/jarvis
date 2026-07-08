"""Query-router decision schema and prompt assembly.

The router is a lightweight *pre-orchestrator* classifier: given the user's raw
query and the resolved runtime snapshot, it decides which service domains the
query actually needs. Its output (a :class:`RouterDecision`) later drives tool-
schema filtering and prompt slimming — but this module owns only the schema and
the prompt text, with no LLM or wiring (see ``router/client.py`` for the call).

The prompt is deliberately small (~300 tokens): the domain keys + their
capabilities pulled from ``DOMAIN_ADAPTERS``, an availability line per domain, a
compact routing-preferences block (mirroring the orchestrator's
``_preference_block``), and a strict JSON-output instruction. It never contains
provider secrets or the full orchestrator policy — the router only needs enough
to route.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from agents.agent_api.app.tools.domain_adapters import DOMAIN_ADAPTERS
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot


class RouterDecision(BaseModel):
    """Structured router output: which domains a query needs, plus an optional rewrite.

    ``domains`` is a subset of the known domain keys (``DOMAIN_ADAPTERS``). An
    empty list means the query needs no service domain (greetings, small talk,
    meta questions). When ``uncertain`` is true, ``domains`` remains the most
    likely minimal route and ``candidate_domains`` is the expanded safe set used
    for tool exposure. ``rewritten_query`` is an optional cleaned-up restatement
    the orchestrator can use instead of the raw text; ``reasoning`` is a short,
    non-authoritative rationale kept only for tracing/debugging.
    """

    model_config = ConfigDict(extra="forbid")

    domains: List[str] = Field(default_factory=list)
    uncertain: bool = False
    candidate_domains: List[str] = Field(default_factory=list)
    rewritten_query: Optional[str] = None
    reasoning: str = ""


def effective_router_domains(decision: RouterDecision) -> List[str]:
    """Return the domain set that should drive tools and prompt slimming."""

    domains = (
        decision.candidate_domains
        if decision.uncertain and decision.candidate_domains
        else decision.domains
    )
    return list(dict.fromkeys(domains))


def _domain_catalogue() -> List[str]:
    """One line per known domain: key, display name, and capabilities."""

    lines: List[str] = []
    for key, adapter in DOMAIN_ADAPTERS.items():
        capabilities = ", ".join(adapter.capabilities) if adapter.capabilities else "—"
        lines.append(f'- "{key}" ({adapter.display_name}): {capabilities}')
    return lines


def _availability_lines(snapshot: RuntimeContextSnapshot) -> List[str]:
    """Mark each known domain connected/not, so the router avoids dead routes."""

    active = snapshot.active_providers()
    lines: List[str] = []
    for key, adapter in DOMAIN_ADAPTERS.items():
        state = "connected" if key in active else "not connected"
        lines.append(f"- {adapter.display_name}: {state}")
    return lines


def _routing_rules(snapshot: RuntimeContextSnapshot) -> List[str]:
    """One numbered, deduplicated block replacing preference + interpretation.

    This merges what used to be three overlapping sections (routing prefs,
    event-provider interpretation, explicit_only guidance) into a single ordered
    ruleset. The model reads one authoritative source instead of the same rule
    stated three ways.
    """

    routing = snapshot.preferences.routing
    rules: List[str] = [
        f"1. Route tasks, to-dos, and projects to `{routing.task_provider}`.",
        f"2. Route events, schedules, availability, free-time, and busy-time requests to `{routing.event_provider}`.",
    ]
    next_index = 3
    if routing.event_provider == "todoist":
        rules.append(
            f"{next_index}. Treat Todoist as able to answer scheduled-item, event, availability, "
            "and free/busy questions — generic calendar or schedule requests route to `todoist`."
        )
        next_index += 1
    if routing.calendar_usage == "explicit_only":
        rules.append(
            f"{next_index}. `google_calendar` is explicit-only: route to it only when the user says "
            "`google calendar`, `google cal`, `gcal`, or `google_calendar`. Generic words like "
            "`calendar`, `schedule`, `free`, or `busy` do NOT trigger `google_calendar`."
        )
        next_index += 1
    rules.append(
        f"{next_index}. Greetings, small talk, and meta questions return an empty `domains` list."
    )
    next_index += 1
    rules.append(
        f"{next_index}. Multi-domain requests: return every domain the request touches."
    )
    next_index += 1
    rules.append(
        f"{next_index}. Prefer connected domains, but if the request clearly needs a disconnected "
        "domain, still return it so the assistant can explain."
    )
    return rules


def _few_shot_examples(snapshot: RuntimeContextSnapshot) -> List[str]:
    """Concrete input/output pairs anchoring the classification pattern.

    These are rendered from the live snapshot so the routed domain in each
    example matches the user's current provider preference — the examples teach
    the *pattern* (task words → task provider, schedule words → event provider),
    not any hardcoded domain key.
    """

    routing = snapshot.preferences.routing
    task_provider = routing.task_provider
    event_provider = routing.event_provider
    examples = [
        f'User: "what tasks do I have today?" -> '
        f'{{"domains": ["{task_provider}"], "uncertain": false, "candidate_domains": [], '
        f'"rewritten_query": null, "reasoning": "task lookup"}}',
        f'User: "what\'s on my schedule this week?" -> '
        f'{{"domains": ["{event_provider}"], "uncertain": false, "candidate_domains": [], '
        f'"rewritten_query": null, "reasoning": "schedule query"}}',
        'User: "hello!" -> '
        '{"domains": [], "uncertain": false, "candidate_domains": [], '
        '"rewritten_query": null, "reasoning": "greeting"}',
    ]
    # Only include the explicit-Google-Calendar example when the domain exists
    # in the adapter catalogue — otherwise it references a domain the model
    # would be told not to emit.
    if "google_calendar" in DOMAIN_ADAPTERS:
        examples.append(
            'User: "add a meeting to my google calendar" -> '
            '{"domains": ["google_calendar"], "uncertain": false, "candidate_domains": [], '
            '"rewritten_query": null, "reasoning": "explicit google calendar mention"}'
        )
    return examples


def _rewrite_rules() -> List[str]:
    """Rules that keep optional rewrites faithful to the raw request."""

    return [
        "1. If the request is already clear, set `rewritten_query` to null.",
        "2. A rewrite must preserve timing modifiers such as `later today`, "
        "`tomorrow`, `next week`, time ranges, and recurrence hints.",
        "3. Do not turn recommendations, comparisons, or planning requests into "
        "pure lookups.",
        "4. Do not add missing task/event details, names, locations, attendees, "
        "durations, or dates.",
        "5. Preserve uncertainty and wording strength: do not turn `maybe`, "
        "`could`, or `which one should` into a definite action.",
    ]


def build_router_system_prompt(snapshot: RuntimeContextSnapshot) -> str:
    """Render the router's system prompt from the resolved snapshot."""

    valid_keys = ", ".join(f'"{key}"' for key in DOMAIN_ADAPTERS)
    return "\n".join(
        [
            "You are a fast query router for a personal assistant. Classify which "
            "service domains the user's request needs. Do not answer the request "
            "or call any tools — only classify.",
            "",
            "## Domains",
            *_domain_catalogue(),
            "",
            "## Connection status",
            *_availability_lines(snapshot),
            "",
            "## Routing rules",
            *_routing_rules(snapshot),
            "",
            "## Examples",
            *_few_shot_examples(snapshot),
            "",
            "## Rewrite rules",
            *_rewrite_rules(),
            "",
            "## Output format",
            "Return exactly one JSON object. No prose, no code fences.",
            "Schema: {",
            f'  "domains": [<subset of {valid_keys}> — most-likely minimal route],',
            '  "uncertain": <boolean — true only for real domain ambiguity>,',
            f'  "candidate_domains": [<subset of {valid_keys}> — expanded safe set when uncertain, else []],',
            '  "rewritten_query": <string or null — faithful restatement, or null if already clear>,',
            '  "reasoning": <short string, 10 words or fewer>',
            "}",
        ]
    )


def build_router_messages(
    query: str,
    snapshot: RuntimeContextSnapshot,
) -> List[Dict[str, Any]]:
    """Build the chat messages (system + user) for one router classification."""

    return [
        {"role": "system", "content": build_router_system_prompt(snapshot)},
        {"role": "user", "content": f"User request:\n{query}"},
    ]


__all__ = [
    "RouterDecision",
    "build_router_messages",
    "build_router_system_prompt",
    "effective_router_domains",
]
