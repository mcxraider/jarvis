#!/usr/bin/env python3
"""Benchmark the TypeSafe (Jev) router over the same grid as ``eval_router.py``.

This is the sibling of ``scripts/eval_router.py``. It reads the same persona
fixtures and the same ``ROUTER_QUERIES`` list, applies the same deterministic
fast-path and guardrail layers, and writes the same three artifacts into the
same directory — only with a ``.typesafe`` infix so the two engines never
collide:

- ``<ts>.typesafe.jsonl``         — one row per (persona, query)
- ``<ts>.typesafe.summary.json``  — run metadata plus latency/token/cost aggregates
- ``<persona>/<ts>.typesafe.md``  — the human-readable per-persona report

Because both scripts share ``load_personas`` and ``load_queries``, the two grids
are identical by construction and rows diff directly on ``(persona, query_index)``.

The rows differ from the LLM engine's in exactly two ways, because the request
shape genuinely differs: ``system_prompt``/``user_prompt`` are replaced by
``request_state``/``request_questions``, and ``raw_answers`` is added so the full
probability distribution behind each decision is recorded, not just the decision
it collapsed to.

**Cost precision.** A TypeSafe router call costs well under $0.0001, which is the
quantum the production telemetry path rounds to — every row would read $0.0000.
Costs here are therefore quantized to 8 decimal places, reported as
``cost_quantum`` in the summary so a comparison against the LLM engine's numbers
is read honestly.

Usage::

    python3 scripts/eval_typesafe_router.py
    python3 scripts/eval_typesafe_router.py --users jerry --queries 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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

# Importing eval_router also loads .env, exactly as the LLM benchmark does.
from scripts.eval_router import (  # noqa: E402
    DEFAULT_OUT_DIR,
    DEFAULT_QUERIES_FILE,
    DEFAULT_USERS_DIR,
    Persona,
    _json_block,
    _utc_now,
    apply_guardrails,
    load_personas,
    load_queries,
)

from agents.agent_api.app.llm.chat import UsageLedger  # noqa: E402
from agents.agent_api.app.pricing import (  # noqa: E402
    calculate_usage_record_cost_usd,
)
from agents.agent_api.app.router.client import RouterClientError  # noqa: E402
from agents.agent_api.app.router.fast_path import fast_path_classify  # noqa: E402
from agents.agent_api.app.router.prompt import RouterDecision  # noqa: E402
from agents.agent_api.app.router.typesafe import (  # noqa: E402
    TYPESAFE_MODEL,
    TypeSafeRouterClient,
    build_typesafe_request,
)
from agents.agent_api.app.tracing import NULL_TRACE  # noqa: E402

API_KEY_ENV = "TYPESAFE_API_KEY"

# Eight decimal places: one call costs ~$0.00003, which the telemetry-wide
# 4-decimal quantum would round to zero.
COST_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class TypeSafeEvalResult:
    persona: Persona
    query_index: int
    query: str
    request_state: Dict[str, Any]
    request_questions: Dict[str, Any]
    raw_response: Dict[str, Any]
    adjusted_response: Optional[Dict[str, Any]]
    raw_answers: Optional[Dict[str, Any]]
    elapsed_ms: float
    error: bool = False
    # Production skips the classifier entirely when the deterministic fast path
    # answers, so these rows must be excludable from latency/cost aggregates.
    fast_path_hit: bool = False
    fast_path_decision: Optional[Dict[str, Any]] = None
    returned_model: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    cost_usd: Optional[str] = None


def _model_payload(decision: RouterDecision) -> Dict[str, Any]:
    return decision.model_dump(mode="json")


def _usage_payload(
    ledger: UsageLedger,
) -> tuple[Optional[str], Optional[Dict[str, int]], Optional[str]]:
    """Flatten the single recorded call into report fields."""

    if not ledger.calls:
        return None, None, None
    record = ledger.calls[0]
    cost = calculate_usage_record_cost_usd(record, quantum=COST_QUANTUM)
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
) -> TypeSafeEvalResult:
    request = build_typesafe_request(query, persona.snapshot)
    fast_path = fast_path_classify(query, persona.snapshot)
    common = {
        "persona": persona,
        "query_index": query_index,
        "query": query,
        "request_state": request["state"],
        "request_questions": request["questions"],
        "fast_path_hit": fast_path is not None,
        "fast_path_decision": (
            _model_payload(fast_path) if fast_path is not None else None
        ),
    }
    ledger = UsageLedger()
    started = time.perf_counter()
    try:
        result = router_client.classify_detailed(
            query,
            persona.snapshot,
            usage_accumulator=ledger,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        adjusted = apply_guardrails(query, persona.snapshot, result.decision)
        raw_payload = _model_payload(result.decision)
        adjusted_payload = _model_payload(adjusted)
        returned_model, usage, cost_usd = _usage_payload(ledger)
        return TypeSafeEvalResult(
            **common,
            raw_response=raw_payload,
            adjusted_response=(
                adjusted_payload if adjusted_payload != raw_payload else None
            ),
            raw_answers=result.answers,
            elapsed_ms=elapsed_ms,
            returned_model=returned_model,
            usage=usage,
            cost_usd=cost_usd,
        )
    except RouterClientError as error:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return TypeSafeEvalResult(
            **common,
            raw_response=error.payload,
            adjusted_response=None,
            raw_answers=None,
            elapsed_ms=elapsed_ms,
            error=True,
        )


def run_grid(
    personas: Sequence[Persona],
    queries: Sequence[tuple[int, str]],
    router_client: Any,
    *,
    verbose: bool = False,
) -> List[TypeSafeEvalResult]:
    results: List[TypeSafeEvalResult] = []
    total = len(personas) * len(queries)
    progress = tqdm(
        total=total,
        desc="Evaluating TypeSafe router",
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
                    adjusted = (
                        result.adjusted_response.get("domains")
                        if result.adjusted_response
                        else None
                    )
                    suffix = (
                        f", adjusted_domains={adjusted}" if adjusted is not None else ""
                    )
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
    results: Sequence[TypeSafeEvalResult],
) -> str:
    """Mirror ``eval_router.format_markdown`` so the two reports read alike."""

    timestamp = run_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: List[str] = [
        f"# TypeSafe router evaluation - {timestamp}",
        "",
        f"**Model:** {model} - **Users:** {len(personas)} - "
        f"**Queries:** {len(queries)} - **Pairs:** {len(results)}",
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
        persona_results = [
            result for result in results if result.persona.name == persona.name
        ]
        for result in persona_results:
            lines.extend(
                [
                    f"### Query {result.query_index}",
                    "**User query:**",
                    f"> {result.query}",
                    "",
                    "**Request (state):**",
                    "```json",
                    _json_block(result.request_state),
                    "```",
                    "",
                    "**Request (questions):**",
                    "```json",
                    _json_block(result.request_questions),
                    "```",
                    "",
                    (
                        "**Response (RouterClientError payload):**"
                        if result.error
                        else "**Response (raw TypeSafe answers):**"
                    ),
                    "```json",
                    _json_block(
                        result.raw_response if result.error else (result.raw_answers or {})
                    ),
                    "```",
                ]
            )
            if not result.error:
                lines.extend(
                    [
                        "**Response (composed RouterDecision):**",
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
                        f"**Tokens:** {usage.get('prompt_tokens', 0)} in / "
                        f"{usage.get('completion_tokens', 0)} out (free) - "
                        f"**Cost:** {f'${result.cost_usd}' if result.cost_usd else 'unpriced'} - "
                        f"**Fast path:** "
                        f"{'hit (production skips the classifier)' if result.fast_path_hit else 'miss'}"
                    ),
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"


def result_row(result: TypeSafeEvalResult) -> Dict[str, Any]:
    """One homogeneous JSONL record, keyed to match ``eval_router.result_row``."""

    usage = result.usage or {}
    questions_json = json.dumps(result.request_questions, sort_keys=True)
    return {
        "persona": result.persona.name,
        "active_providers": list(result.persona.active_providers),
        "query_index": result.query_index,
        "query": result.query,
        "request_state": result.request_state,
        "request_questions": result.request_questions,
        # The questions are rendered from live preferences and DOMAIN_ADAPTERS, so
        # they can drift between runs. This proves two runs compared the same
        # input before their latency/cost numbers are compared.
        "request_questions_sha256": hashlib.sha256(
            questions_json.encode("utf-8")
        ).hexdigest(),
        "fast_path_hit": result.fast_path_hit,
        "fast_path_decision": result.fast_path_decision,
        "raw_answers": result.raw_answers,
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
    personas: Sequence[Persona],
    queries: Sequence[tuple[int, str]],
    results: Sequence[TypeSafeEvalResult],
) -> Dict[str, Any]:
    """Same aggregate keys as ``eval_router.summarize`` so the files diff."""

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
        "engine": "typesafe",
        "provider": getattr(getattr(profile, "provider", None), "value", None),
        "requested_model": getattr(router_client, "model", "unknown"),
        "reasoning_effort": getattr(router_client, "reasoning_effort", None),
        "base_url": getattr(router_client, "base_url", None),
        "request_timeout_seconds": getattr(
            router_client, "request_timeout_seconds", None
        ),
        "max_retry_attempts": getattr(router_client, "max_retry_attempts", None),
        "personas": len(personas),
        "queries": len(queries),
        "pairs": len(results),
        "errors": sum(1 for result in results if result.error),
        "fast_path_hits": sum(1 for result in results if result.fast_path_hit),
        "guardrail_adjustments": sum(
            1 for result in results if result.adjusted_response is not None
        ),
        # Aggregates cover successful non-fast-path calls only — the requests
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
        "cost_quantum": str(COST_QUANTUM),
        "total_cost_usd": None if total_cost is None else str(total_cost),
        "mean_cost_usd_per_call": (
            None
            if total_cost is None or not billable
            else str(total_cost / Decimal(len(billable)))
        ),
    }


def _stamp(run_at: datetime) -> str:
    return run_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_jsonl(
    results: Sequence[TypeSafeEvalResult],
    out_dir: Path,
    *,
    run_at: datetime,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_stamp(run_at)}.typesafe.jsonl"
    path.write_text(
        "".join(
            json.dumps(result_row(result), sort_keys=True) + "\n" for result in results
        ),
        encoding="utf-8",
    )
    return path


def write_summary(summary: Dict[str, Any], out_dir: Path, *, run_at: datetime) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_stamp(run_at)}.typesafe.summary.json"
    path.write_text(_json_block(summary) + "\n", encoding="utf-8")
    return path


def write_report(
    markdown: str,
    out_dir: Path,
    *,
    persona_name: str,
    run_at: datetime,
) -> Path:
    persona_dir = out_dir / persona_name
    persona_dir.mkdir(parents=True, exist_ok=True)
    path = persona_dir / f"{_stamp(run_at)}.typesafe.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def write_persona_reports(
    *,
    run_at: datetime,
    model: str,
    personas: Sequence[Persona],
    queries: Sequence[tuple[int, str]],
    results: Sequence[TypeSafeEvalResult],
    out_dir: Path,
) -> List[Path]:
    report_paths: List[Path] = []
    for persona in personas:
        persona_results = [
            result for result in results if result.persona.name == persona.name
        ]
        markdown = format_markdown(
            run_at=run_at,
            model=model,
            personas=[persona],
            queries=queries,
            results=persona_results,
        )
        report_paths.append(
            write_report(markdown, out_dir, persona_name=persona.name, run_at=run_at)
        )
    return report_paths


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the TypeSafe router persona x query eval grid."
    )
    parser.add_argument("--users-dir", type=Path, default=DEFAULT_USERS_DIR)
    parser.add_argument("--queries-file", type=Path, default=DEFAULT_QUERIES_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--users", nargs="*", help="Persona names to include, e.g. --users jerry zac"
    )
    parser.add_argument(
        "--queries",
        nargs="*",
        help="1-based query numbers or text filters, e.g. --queries 1",
    )
    parser.add_argument("--model", default=TYPESAFE_MODEL)
    parser.add_argument(
        "--quiet", action="store_true", help="Hide progress bars and detailed run logs."
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    api_key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not api_key:
        raise SystemExit(f"No TypeSafe API key resolved. Set {API_KEY_ENV}.")

    verbose = not args.quiet
    if verbose:
        tqdm.write("Loading router eval fixtures...")
        tqdm.write(f"Users dir: {args.users_dir}")
        tqdm.write(f"Queries file: {args.queries_file}")
    personas = load_personas(args.users_dir, user_filters=args.users)
    queries = load_queries(args.queries_file, query_filters=args.queries)

    run_at = _utc_now()
    with TypeSafeRouterClient(
        api_key, model=args.model, tracer=NULL_TRACE
    ) as router_client:
        if verbose:
            tqdm.write(
                f"Loaded {len(personas)} persona(s), {len(queries)} query/queries, "
                f"{len(personas) * len(queries)} eval pair(s)."
            )
            tqdm.write(f"TypeSafe model: {router_client.model}")
        results = run_grid(personas, queries, router_client, verbose=verbose)
        summary = summarize(
            run_at=run_at,
            router_client=router_client,
            personas=personas,
            queries=queries,
            results=results,
        )

    jsonl_path = write_jsonl(results, args.out_dir, run_at=run_at)
    summary_path = write_summary(summary, args.out_dir, run_at=run_at)
    tqdm.write(_json_block(summary))
    tqdm.write(f"Wrote TypeSafe benchmark records: {jsonl_path}")
    tqdm.write(f"Wrote TypeSafe benchmark summary: {summary_path}")
    if verbose:
        tqdm.write("Formatting markdown reports by persona...")
    report_paths = write_persona_reports(
        run_at=run_at,
        model=args.model,
        personas=personas,
        queries=queries,
        results=results,
        out_dir=args.out_dir,
    )
    for report_path in report_paths:
        tqdm.write(f"Wrote TypeSafe eval report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
