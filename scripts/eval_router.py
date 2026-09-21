#!/usr/bin/env python3
"""Evaluate and benchmark router decisions across persona and query fixtures.

This harness intentionally bypasses Supabase and constructs the router's real
input directly: a query string plus a RuntimeContextSnapshot. The live path uses
RouterClient, while tests can inject a fake client and exercise everything else
without making an API call.

Fill in ``PROMPTS`` below to benchmark your own queries; leave it empty to fall
back to the fixture corpus in ``--queries-file``.

Each run writes to ``--out-dir``:

- ``router_results.json`` — the cumulative benchmark record. Every run *appends*
  one row per (persona, query) to this single JSON array: the user prompt, the
  router's decision, the guardrail-adjusted decision, latency, token counts, and
  exact USD cost. The system prompt itself is not stored, only its SHA-256.
- ``<ts>.summary.json`` — run metadata (provider, model, reasoning, timeouts) plus
  latency percentiles, token totals, and total cost over the calls production
  would really have issued (successes that the deterministic fast path missed).
- ``<persona>/<ts>.md`` — the human-readable per-persona report, system prompt included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is pinned for this repo.
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")

from agents.agent_api.app.config import settings
from agents.agent_api.app.llm.chat import UsageLedger
from agents.agent_api.app.pricing import calculate_usage_record_cost_usd
from agents.agent_api.app.router.client import RouterClient, RouterClientError
from agents.agent_api.app.router.fast_path import fast_path_classify
from agents.agent_api.app.router.prompt import RouterDecision, build_router_messages
from agents.agent_api.app.tools.control import ASK_USER_TOOL_NAME
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from agents.agent_api.app.tracing import NULL_TRACE
from agents.agent_api.app.user_context.preferences import AssistantPreferencesV1
from agents.agent_api.app.user_context.runtime import (
    DomainAvailability,
    RuntimeContextSnapshot,
)
from tests.agents.runtime_helpers import _CAPABILITIES, _TOOL_NAMES

DEFAULT_USERS_DIR = ROOT / "tests" / "data" / "router_users"
DEFAULT_QUERIES_FILE = ROOT / "tests" / "data" / "router_queries.py"
DEFAULT_OUT_DIR = ROOT / "tests" / "data" / "router_evals"
RESULTS_FILENAME = "router_results.json"

# Prompts to benchmark. Fill this in; leave empty to fall back to --queries-file.
PROMPTS: List[str] = [
    "16 nov am i free",
    "deadline for company A OA: 7 days add in for me",
    "check my scheule tmr",
    "whats on my google cal tmr",
    "call ming kai to book hircut tmr",
    "bfast w zac delete this",
    "ad in for me and feebee do pilates",
    "check all events on todoist and put them in google cal",
    "whats on my govtech calendar",
    "whats on for my work tmr.",
    "whats the weather tmr",
    "search on google if lebron is alive",
]


@dataclass(frozen=True)
class Persona:
    name: str
    path: Path
    snapshot: RuntimeContextSnapshot
    active_providers: List[str]


@dataclass(frozen=True)
class EvalResult:
    persona: Persona
    query_index: int
    query: str
    system_prompt: str
    user_prompt: str
    raw_response: dict[str, Any]
    adjusted_response: Optional[dict[str, Any]]
    elapsed_ms: float
    error: bool = False
    # Production skips the LLM entirely when the deterministic fast path answers,
    # so these rows must be excludable from latency/cost aggregates.
    fast_path_hit: bool = False
    fast_path_decision: Optional[dict[str, Any]] = None
    returned_model: Optional[str] = None
    usage: Optional[dict[str, int]] = None
    cost_usd: Optional[str] = None


class _GuardrailClient:
    def classify(self, query: str, snapshot: RuntimeContextSnapshot) -> RouterDecision:
        raise AssertionError("guardrail-only client should never classify")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_block(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _model_payload(decision: RouterDecision) -> dict[str, Any]:
    return decision.model_dump(mode="json")


def _split_filter_values(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return []
    filters: List[str] = []
    for value in values:
        filters.extend(part.strip() for part in value.split(",") if part.strip())
    return filters


def build_snapshot_from_fixture(data: dict[str, Any], *, name: str = "fixture") -> RuntimeContextSnapshot:
    """Validate fixture JSON and build the exact snapshot consumed by the router."""

    preferences = AssistantPreferencesV1.model_validate(data["preferences"])
    active = set(data.get("active_providers", []))
    domains: List[DomainAvailability] = []
    registered_tools: List[str] = [ASK_USER_TOOL_NAME]

    for provider in ("todoist", "google_calendar"):
        capabilities = list(_CAPABILITIES[provider])
        tool_names = list(_TOOL_NAMES[provider])
        if provider in active:
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="active",
                    connection_id=f"{name}-{provider}-conn",
                    capabilities=capabilities,
                    tool_names=tool_names,
                )
            )
            registered_tools.extend(tool_names)
        else:
            domains.append(
                DomainAvailability(
                    provider=provider,
                    status="unavailable",
                    reason="not_connected",
                    capabilities=capabilities,
                    tool_names=tool_names,
                )
            )

    return RuntimeContextSnapshot(
        user_id=f"router-eval-{name}",
        display_name=data["display_name"],
        timezone=data.get("timezone", "UTC"),
        locale=data.get("locale", "en"),
        preference_schema_version=1,
        preference_revision=1,
        preferences=preferences,
        domains=domains,
        registered_tools=registered_tools,
        resolved_at=_utc_now(),
    )


def load_personas(users_dir: Path, *, user_filters: Optional[Sequence[str]] = None) -> List[Persona]:
    filters = {value.lower() for value in _split_filter_values(user_filters)}
    personas: List[Persona] = []
    for path in sorted(users_dir.glob("*.json")):
        name = path.stem
        if filters and name.lower() not in filters:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        snapshot = build_snapshot_from_fixture(data, name=name)
        personas.append(
            Persona(
                name=name,
                path=path,
                snapshot=snapshot,
                active_providers=list(data.get("active_providers", [])),
            )
        )
    if not personas:
        requested = ", ".join(sorted(filters)) if filters else str(users_dir)
        raise ValueError(f"No router persona fixtures matched: {requested}")
    return personas


def select_queries(
    raw_queries: Sequence[str],
    *,
    query_filters: Optional[Sequence[str]] = None,
) -> List[tuple[int, str]]:
    """Number queries 1-based and apply --queries index/substring filters."""

    queries = [(index, query) for index, query in enumerate(raw_queries, start=1)]

    filters = _split_filter_values(query_filters)
    if not filters:
        return queries

    selected: List[tuple[int, str]] = []
    for value in filters:
        if value.isdigit():
            target = int(value)
            selected.extend(item for item in queries if item[0] == target)
        else:
            lowered = value.lower()
            selected.extend(item for item in queries if lowered in item[1].lower())

    deduped = list(dict.fromkeys(selected))
    if not deduped:
        raise ValueError(f"No router queries matched: {', '.join(filters)}")
    return deduped


def load_queries(
    queries_file: Path,
    *,
    query_filters: Optional[Sequence[str]] = None,
) -> List[tuple[int, str]]:
    module_globals = runpy.run_path(str(queries_file))
    raw_queries = module_globals.get("ROUTER_QUERIES")
    if not isinstance(raw_queries, list) or not all(isinstance(query, str) for query in raw_queries):
        raise ValueError(f"{queries_file} must define ROUTER_QUERIES as a list of strings")
    return select_queries(raw_queries, query_filters=query_filters)


def apply_guardrails(query: str, snapshot: RuntimeContextSnapshot, decision: RouterDecision) -> RouterDecision:
    selector = RouterToolSelector(
        router_client=_GuardrailClient(),
        snapshot=snapshot,
        tracer=NULL_TRACE,
    )
    return selector._apply_routing_guardrails(query, decision)


def _usage_payload(ledger: UsageLedger) -> tuple[Optional[str], Optional[Dict[str, int]], Optional[str]]:
    """Flatten the single router call recorded on the ledger into report fields.

    Returns ``(returned_model, token_counts, cost_usd)``. Cost is a decimal
    string, never a float, and stays ``None`` when ``pricing.py`` has no
    maintained rate for the provider/model — it deliberately never guesses.
    """

    if not ledger.calls:
        return None, None, None
    record = ledger.calls[0]
    cost = calculate_usage_record_cost_usd(record)
    return (
        record.returned_model,
        {
            "prompt_tokens": record.prompt_tokens,
            "cached_read_tokens": record.cached_read_tokens,
            "cache_write_tokens": record.cache_write_tokens,
            "completion_tokens": record.completion_tokens,
            "reasoning_tokens": record.reasoning_tokens,
        },
        None if cost is None else str(cost),
    )


def evaluate_pair(
    persona: Persona,
    query_index: int,
    query: str,
    router_client: Any,
) -> EvalResult:
    messages = build_router_messages(query, persona.snapshot)
    fast_path = fast_path_classify(query, persona.snapshot)
    common = {
        "persona": persona,
        "query_index": query_index,
        "query": query,
        "system_prompt": messages[0]["content"],
        "user_prompt": messages[1]["content"],
        "fast_path_hit": fast_path is not None,
        "fast_path_decision": _model_payload(fast_path) if fast_path is not None else None,
    }
    ledger = UsageLedger()
    started = time.perf_counter()
    try:
        decision = router_client.classify(
            query,
            persona.snapshot,
            usage_accumulator=ledger,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        adjusted = apply_guardrails(query, persona.snapshot, decision)
        raw_payload = _model_payload(decision)
        adjusted_payload = _model_payload(adjusted)
        returned_model, usage, cost_usd = _usage_payload(ledger)
        return EvalResult(
            **common,
            raw_response=raw_payload,
            adjusted_response=adjusted_payload if adjusted_payload != raw_payload else None,
            elapsed_ms=elapsed_ms,
            returned_model=returned_model,
            usage=usage,
            cost_usd=cost_usd,
        )
    except RouterClientError as error:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return EvalResult(
            **common,
            raw_response=error.payload,
            adjusted_response=None,
            elapsed_ms=elapsed_ms,
            error=True,
        )


def run_grid(
    personas: Sequence[Persona],
    queries: Sequence[tuple[int, str]],
    router_client: Any,
    *,
    verbose: bool = False,
) -> List[EvalResult]:
    results: List[EvalResult] = []
    total = len(personas) * len(queries)
    progress = tqdm(
        total=total,
        desc="Evaluating router",
        unit="pair",
        disable=not verbose,
    )
    with progress:
        for persona in personas:
            if verbose:
                active = ", ".join(persona.active_providers) or "none"
                tqdm.write(f"Persona {persona.name}: active providers={active}")
            for query_index, query in queries:
                progress.set_postfix(persona=persona.name, query=query_index)
                result = evaluate_pair(persona, query_index, query, router_client)
                results.append(result)
                if verbose:
                    status = "error" if result.error else "ok"
                    domains = result.raw_response.get("domains", [])
                    adjusted = result.adjusted_response.get("domains") if result.adjusted_response else None
                    suffix = f", adjusted_domains={adjusted}" if adjusted is not None else ""
                    tqdm.write(
                        f"[{status}] {persona.name} q{query_index}: "
                        f"domains={domains}{suffix} elapsed={result.elapsed_ms}ms"
                    )
                progress.update(1)
    return results


def format_markdown(
    *,
    run_at: datetime,
    model: str,
    personas: Sequence[Persona],
    queries: Sequence[tuple[int, str]],
    results: Sequence[EvalResult],
) -> str:
    timestamp = run_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: List[str] = [
        f"# Router evaluation - {timestamp}",
        "",
        f"**Model:** {model} - **Users:** {len(personas)} - **Queries:** {len(queries)} - **Pairs:** {len(results)}",
        "",
        "---",
        "",
    ]

    for persona in personas:
        routing = persona.snapshot.preferences.routing
        active = ", ".join(persona.active_providers) or "none"
        lines.extend(
            [
                f"## {persona.name}",
                (
                    f"Task provider: {routing.task_provider} - "
                    f"Event provider: {routing.event_provider} - "
                    f"Reminder provider: {routing.reminder_provider} - "
                    f"Time-related provider: {routing.time_related_provider} - "
                    f"Explicit calendar provider: "
                    f"{routing.explicit_calendar_provider} - "
                    f"calendar_usage: {routing.calendar_usage} - Active: {active}"
                ),
                "",
            ]
        )
        persona_results = [result for result in results if result.persona.name == persona.name]
        for result in persona_results:
            lines.extend(
                [
                    f"### Query {result.query_index}",
                    "**User query:**",
                    f"> {result.query}",
                    "",
                    "**Prompt (system):**",
                    "```text",
                    result.system_prompt,
                    "```",
                    "",
                    "**Prompt (user):**",
                    "```text",
                    result.user_prompt,
                    "```",
                    "",
                    (
                        "**Response (RouterClientError payload):**"
                        if result.error
                        else "**Response (raw RouterDecision):**"
                    ),
                    "```json",
                    _json_block(result.raw_response),
                    "```",
                ]
            )
            if result.adjusted_response is not None:
                lines.extend(
                    [
                        "**Response (after guardrails):**",
                        "```json",
                        _json_block(result.adjusted_response),
                        "```",
                    ]
                )
            usage = result.usage or {}
            lines.extend(
                [
                    (
                        f"**Elapsed:** {result.elapsed_ms} ms - "
                        f"**Model:** {result.returned_model or 'unknown'} - "
                        f"**Tokens:** {usage.get('prompt_tokens', 0)} in "
                        f"({usage.get('cached_read_tokens', 0)} cached) / "
                        f"{usage.get('completion_tokens', 0)} out - "
                        f"**Cost:** {f'${result.cost_usd}' if result.cost_usd else 'unpriced'} - "
                        f"**Fast path:** {'hit (production skips the LLM)' if result.fast_path_hit else 'miss'}"
                    ),
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"


def result_row(result: EvalResult, *, run_at: datetime) -> Dict[str, Any]:
    """One homogeneous record: router input, output, latency, tokens, cost.

    The system prompt is deliberately omitted — it is ~750 identical tokens per
    row. Only its hash is kept, and the per-persona Markdown report carries the
    full text when you need to read it.
    """

    usage = result.usage or {}
    return {
        "run_at": run_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "persona": result.persona.name,
        "active_providers": list(result.persona.active_providers),
        "query_index": result.query_index,
        "query": result.query,
        "user_prompt": result.user_prompt,
        # The prompt is rendered from live preferences and DOMAIN_ADAPTERS, so it
        # can drift between runs. This is what proves two runs compared the same
        # input before their latency/cost numbers are compared.
        "system_prompt_sha256": hashlib.sha256(
            result.system_prompt.encode("utf-8")
        ).hexdigest(),
        "fast_path_hit": result.fast_path_hit,
        "fast_path_decision": result.fast_path_decision,
        "decision": None if result.error else result.raw_response,
        "decision_adjusted": result.adjusted_response,
        "latency_ms": result.elapsed_ms,
        "returned_model": result.returned_model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_read_tokens": usage.get("cached_read_tokens"),
        "cache_write_tokens": usage.get("cache_write_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "cost_usd": result.cost_usd,
        "error": result.raw_response if result.error else None,
    }


def summarize(
    *,
    run_at: datetime,
    router_client: Any,
    tracing_enabled: bool,
    personas: Sequence[Persona],
    queries: Sequence[tuple[int, str]],
    results: Sequence[EvalResult],
) -> Dict[str, Any]:
    """Run metadata plus aggregates over the calls production would actually make."""

    profile = getattr(router_client, "profile", None)
    billable = [
        result for result in results if not result.error and not result.fast_path_hit
    ]
    latencies = sorted(result.elapsed_ms for result in billable)
    costs = [result.cost_usd for result in billable]
    total_cost = (
        sum((Decimal(cost) for cost in costs), Decimal("0"))
        if costs and all(cost is not None for cost in costs)
        else None
    )

    def total(field: str) -> int:
        return sum((result.usage or {}).get(field, 0) for result in billable)

    def percentile(fraction: float) -> Optional[float]:
        if not latencies:
            return None
        index = min(len(latencies) - 1, int(round(fraction * (len(latencies) - 1))))
        return latencies[index]

    return {
        "run_at": run_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": getattr(getattr(profile, "provider", None), "value", None),
        "requested_model": getattr(router_client, "model", "unknown"),
        "reasoning_effort": getattr(router_client, "reasoning_effort", None),
        "base_url": getattr(router_client, "base_url", None),
        "request_timeout_seconds": getattr(
            router_client, "request_timeout_seconds", None
        ),
        "max_retry_attempts": getattr(router_client, "max_retry_attempts", None),
        "langsmith_tracing": tracing_enabled,
        "personas": len(personas),
        "queries": len(queries),
        "pairs": len(results),
        "errors": sum(1 for result in results if result.error),
        "fast_path_hits": sum(1 for result in results if result.fast_path_hit),
        "guardrail_adjustments": sum(
            1 for result in results if result.adjusted_response is not None
        ),
        # Aggregates cover successful non-fast-path calls only — the LLM requests
        # production would really have issued.
        "llm_calls": len(billable),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 1) if latencies else None,
            "p50": percentile(0.5),
            "p95": percentile(0.95),
            "min": latencies[0] if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
        "tokens": {
            "prompt": total("prompt_tokens"),
            "cached_read": total("cached_read_tokens"),
            "completion": total("completion_tokens"),
            "reasoning": total("reasoning_tokens"),
        },
        "total_cost_usd": None if total_cost is None else str(total_cost),
        "mean_cost_usd_per_call": (
            None
            if total_cost is None or not billable
            else str(total_cost / Decimal(len(billable)))
        ),
    }


def append_results(results: Sequence[EvalResult], out_dir: Path, *, run_at: datetime) -> Path:
    """Append this run's rows to the one cumulative benchmark array."""

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / RESULTS_FILENAME
    # ponytail: read-modify-write the whole array; fine at 168-rows-per-run scale,
    # switch to line-appended .jsonl if the file ever gets big enough to notice.
    rows: List[Dict[str, Any]] = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    )
    rows.extend(result_row(result, run_at=run_at) for result in results)
    path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_summary(summary: Dict[str, Any], out_dir: Path, *, run_at: datetime) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = run_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{timestamp}.summary.json"
    path.write_text(_json_block(summary) + "\n", encoding="utf-8")
    return path


def write_report(markdown: str, out_dir: Path, *, persona_name: str, run_at: datetime) -> Path:
    persona_dir = out_dir / persona_name
    persona_dir.mkdir(parents=True, exist_ok=True)
    timestamp = run_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = persona_dir / f"{timestamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def write_persona_reports(
    *,
    run_at: datetime,
    model: str,
    personas: Sequence[Persona],
    queries: Sequence[tuple[int, str]],
    results: Sequence[EvalResult],
    out_dir: Path,
) -> List[Path]:
    report_paths: List[Path] = []
    for persona in personas:
        persona_results = [result for result in results if result.persona.name == persona.name]
        markdown = format_markdown(
            run_at=run_at,
            model=model,
            personas=[persona],
            queries=queries,
            results=persona_results,
        )
        report_paths.append(write_report(markdown, out_dir, persona_name=persona.name, run_at=run_at))
    return report_paths


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the live router persona x query eval grid.")
    parser.add_argument("--users-dir", type=Path, default=DEFAULT_USERS_DIR)
    parser.add_argument("--queries-file", type=Path, default=DEFAULT_QUERIES_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--users", nargs="*", help="Persona names to include, e.g. --users jerry zac")
    parser.add_argument("--queries", nargs="*", help="1-based query numbers or text filters, e.g. --queries 1")
    parser.add_argument("--quiet", action="store_true", help="Hide progress bars and detailed run logs.")
    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Keep LangSmith tracing on. Off by default: wrap_openai/@traceable add "
            "per-call work on the hot path, which a latency benchmark should not measure."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    # Check the key the router will actually use, not one provider's env var:
    # ROUTER_API_KEY is optional and the profile inherits the provider key
    # (OPENAI_API_KEY or DEEPSEEK_API_KEY) selected by ROUTER_PROVIDER/LLM_PROVIDER.
    if not (settings.router_api_key or "").strip():
        raise SystemExit(
            "No router API key resolved. Set ROUTER_API_KEY, or the API key for "
            "the provider selected by ROUTER_PROVIDER/LLM_PROVIDER."
        )

    if not args.trace:
        # langsmith reads these per call, so setting them before the grid runs is
        # enough — no import-order dance needed.
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ["LANGCHAIN_TRACING_V2"] = "false"

    verbose = not args.quiet
    if verbose:
        tqdm.write("Loading router eval fixtures...")
        tqdm.write(f"Users dir: {args.users_dir}")
        tqdm.write(
            "Prompts: PROMPTS list in this script"
            if PROMPTS
            else f"Prompts: {args.queries_file} (PROMPTS is empty)"
        )
    personas = load_personas(args.users_dir, user_filters=args.users)
    queries = (
        select_queries(PROMPTS, query_filters=args.queries)
        if PROMPTS
        else load_queries(args.queries_file, query_filters=args.queries)
    )
    router_client = RouterClient(tracer=NULL_TRACE)
    if verbose:
        tqdm.write(
            f"Loaded {len(personas)} persona(s), {len(queries)} query/queries, "
            f"{len(personas) * len(queries)} eval pair(s)."
        )
        tqdm.write(f"Router model: {getattr(router_client, 'model', 'unknown')}")
    run_at = _utc_now()
    results = run_grid(personas, queries, router_client, verbose=verbose)
    summary = summarize(
        run_at=run_at,
        router_client=router_client,
        tracing_enabled=args.trace,
        personas=personas,
        queries=queries,
        results=results,
    )
    results_path = append_results(results, args.out_dir, run_at=run_at)
    summary_path = write_summary(summary, args.out_dir, run_at=run_at)
    tqdm.write(_json_block(summary))
    tqdm.write(f"Appended {len(results)} router benchmark record(s): {results_path}")
    tqdm.write(f"Wrote router benchmark summary: {summary_path}")
    if verbose:
        tqdm.write("Formatting markdown reports by persona...")
    report_paths = write_persona_reports(
        run_at=run_at,
        model=getattr(router_client, "model", "unknown"),
        personas=personas,
        queries=queries,
        results=results,
        out_dir=args.out_dir,
    )
    for report_path in report_paths:
        tqdm.write(f"Wrote router eval report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
