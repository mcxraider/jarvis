"""Query-router decision schema and prompt assembly.

The router is a lightweight *pre-orchestrator* classifier: given the user's raw
query and the resolved runtime snapshot, it decides which service domains the
query actually needs and how intrinsically complex the query is. Its output (a
:class:`RouterDecision`) later drives tool-schema filtering, prompt slimming,
and model routing — but this module owns only the schema and the prompt text,
with no LLM or wiring (see ``router/client.py`` for the call).

The prompt is deliberately compact: the domain keys + their capabilities pulled
from ``DOMAIN_ADAPTERS``, an availability line per domain, a compact routing-
preferences block (mirroring the orchestrator's ``_preference_block``), a query-
complexity rubric, and a strict JSON-output instruction. It never contains
provider secrets or the full orchestrator policy — the router only needs enough
to classify.
"""

import hashlib
import json
from enum import Enum
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.agent_api.app.tools.domain_adapters import DOMAIN_ADAPTERS
from agents.agent_api.app.user_context.runtime import RuntimeContextSnapshot


class RouterDomain(str, Enum):
    """Service-domain identifiers accepted from the router model."""

    TODOIST = "todoist"
    GOOGLE_CALENDAR = "google_calendar"


class RouterOutcome(str, Enum):
    ROUTED = "routed"
    CONVERSATION = "conversation"
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    AMBIGUOUS = "ambiguous"


class QueryComplexity(str, Enum):
    """Intrinsic reasoning complexity of the current user query."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


if {member.value for member in RouterDomain} != set(DOMAIN_ADAPTERS):
    raise RuntimeError("RouterDomain must exactly match DOMAIN_ADAPTERS keys")


class RouterDecision(BaseModel):
    """Strict structured classification returned by the query router."""

    model_config = ConfigDict(extra="forbid")

    outcome: RouterOutcome
    domains: List[RouterDomain] = Field(strict=True)
    uncertain: bool
    candidate_domains: List[RouterDomain] = Field(strict=True)
    complexity: QueryComplexity
    reasoning: str

    @model_validator(mode="after")
    def validate_consistency(self) -> "RouterDecision":
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("domains must not contain duplicates")
        if len(set(self.candidate_domains)) != len(self.candidate_domains):
            raise ValueError("candidate_domains must not contain duplicates")
        if self.outcome == RouterOutcome.ROUTED and not self.domains:
            raise ValueError("routed outcome requires at least one domain")
        if self.outcome != RouterOutcome.ROUTED and self.domains:
            raise ValueError("non-routed outcomes require an empty domains list")
        if not self.uncertain and self.candidate_domains:
            raise ValueError("candidate_domains must be empty when uncertain is false")
        if self.uncertain:
            if not self.candidate_domains:
                raise ValueError("uncertain decisions require candidate_domains")
            if not set(self.domains).issubset(self.candidate_domains):
                raise ValueError("candidate_domains must contain every routed domain")
        if self.outcome == RouterOutcome.AMBIGUOUS and not self.uncertain:
            raise ValueError("ambiguous outcome must be uncertain")
        return self


def effective_router_domains(decision: RouterDecision) -> List[str]:
    """Return the domain set that should drive tools and prompt slimming."""

    domains = (
        decision.candidate_domains
        if decision.uncertain and decision.candidate_domains
        else decision.domains
    )
    return [domain.value for domain in domains]


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
        f"{next_index}. Greetings, small talk, and meta questions use outcome `conversation` and empty domains."
    )
    next_index += 1
    rules.append(
        f"{next_index}. Requests explicitly targeting an unlisted provider use outcome "
        "`unsupported_provider` and empty domains."
    )
    next_index += 1
    rules.append(
        f"{next_index}. If service access is needed but the domain is genuinely unclear, use "
        "outcome `ambiguous`, set `uncertain` true, and return the safe candidate domains."
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
        f'{{"outcome": "routed", "domains": ["{task_provider}"], "uncertain": false, '
        f'"candidate_domains": [], "complexity": "low", "reasoning": "task lookup"}}',
        f'User: "what\'s on my schedule this week?" -> '
        f'{{"outcome": "routed", "domains": ["{event_provider}"], "uncertain": false, '
        f'"candidate_domains": [], "complexity": "low", "reasoning": "schedule query"}}',
        'User: "hello!" -> '
        '{"outcome": "conversation", "domains": [], "uncertain": false, '
        '"candidate_domains": [], "complexity": "low", "reasoning": "greeting"}',
        'User: "check my Slack messages" -> '
        '{"outcome": "unsupported_provider", "domains": [], "uncertain": false, '
        '"candidate_domains": [], "complexity": "low", "reasoning": "unsupported Slack request"}',
        'User: "check my plans somewhere" -> '
        '{"outcome": "ambiguous", "domains": [], "uncertain": true, '
        '"candidate_domains": ["todoist", "google_calendar"], "complexity": "low", '
        '"reasoning": "service domain unclear"}',
        'User: "which overdue tasks should I do first today?" -> '
        f'{{"outcome": "routed", "domains": ["{task_provider}"], "uncertain": false, '
        '"candidate_domains": [], "complexity": "medium", "reasoning": "prioritize overdue tasks"}',
        'User: "analyze all my projects and build an optimized monthly execution plan" -> '
        f'{{"outcome": "routed", "domains": ["{task_provider}"], "uncertain": false, '
        '"candidate_domains": [], "complexity": "high", "reasoning": "complex project optimization"}',
    ]
    # Only include the explicit-Google-Calendar example when the domain exists
    # in the adapter catalogue — otherwise it references a domain the model
    # would be told not to emit.
    if "google_calendar" in DOMAIN_ADAPTERS:
        examples.append(
            'User: "add a meeting to my google calendar" -> '
            '{"outcome": "routed", "domains": ["google_calendar"], "uncertain": false, '
            '"candidate_domains": [], "complexity": "low", '
            '"reasoning": "explicit google calendar mention"}'
        )
    return examples


def build_router_system_prompt(snapshot: RuntimeContextSnapshot) -> str:
    """Render the router's system prompt from the resolved snapshot."""

    valid_keys = ", ".join(f'"{key}"' for key in DOMAIN_ADAPTERS)
    return "\n".join(
        [
            "You are a fast query router for a personal assistant. Classify which "
            "service domains the user's request needs and the intrinsic complexity "
            "of the current user query. Do not answer the request or call any tools "
            "— only classify.",
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
            "## Query complexity",
            "Classify the intrinsic reasoning and workflow difficulty of the current user query:",
            "- `low`: a direct lookup, simple conversation, or straightforward single-item action.",
            "- `medium`: multiple steps, items, comparisons, constraints, or moderate synthesis.",
            "- `high`: complex planning, optimization, substantial analysis, or many interdependent constraints.",
            "Judge complexity independently of the selected domains, number of domains, query length, "
            "or mutation risk. Domain breadth is handled separately by deterministic model routing.",
            "",
            "## Examples",
            *_few_shot_examples(snapshot),
            "",
            "## Output format",
            "Return exactly one JSON object. No prose, no code fences.",
            "Schema: {",
            '  "outcome": <one of "routed", "conversation", "unsupported_provider", "ambiguous">,',
            f'  "domains": [<subset of {valid_keys}> — most-likely minimal route],',
            '  "uncertain": <boolean — true only for real domain ambiguity>,',
            f'  "candidate_domains": [<subset of {valid_keys}> — expanded safe set when uncertain, else []],',
            '  "complexity": <one of "low", "medium", "high" — intrinsic difficulty of the current user query>,',
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


def router_prompt_schema_fingerprint(snapshot: RuntimeContextSnapshot) -> str:
    """Fingerprint the rendered prompt and strict response schema for cache safety."""

    schema = json.dumps(
        RouterDecision.model_json_schema(),
        sort_keys=True,
        separators=(",", ":"),
    )
    contract = f"{build_router_system_prompt(snapshot)}\n{schema}"
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()


__all__ = [
    "RouterDecision",
    "RouterDomain",
    "RouterOutcome",
    "QueryComplexity",
    "build_router_messages",
    "build_router_system_prompt",
    "effective_router_domains",
    "router_prompt_schema_fingerprint",
]
