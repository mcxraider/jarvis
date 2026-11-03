#!/usr/bin/env python3
"""Compare Jev, the LLM baseline, and their cascade against curated labels.

No tools or user database are accessed. Output includes private query text: keep
artifacts local. Replay evaluates recorded 1.13 answers, not current Jev quality.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.agent_api.app.config import settings
from agents.agent_api.app.llm.chat import UsageLedger
from agents.agent_api.app.pricing import calculate_usage_record_cost_usd
from agents.agent_api.app.router.client import RouterClient, RouterClientError
from agents.agent_api.app.router.prompt import RouterDecision, effective_router_domains
from agents.agent_api.app.router.runtime import RoutingChain
from agents.agent_api.app.router.typesafe import (
    TypeSafeRouterClient,
    decision_from_answers,
)
from agents.agent_api.app.tracing import TracePrinter
from scripts.eval_router import DEFAULT_USERS_DIR, load_personas


class EvaluationTrace(TracePrinter):
    def __init__(self):
        super().__init__(enabled=False)
        self.fallbacks = 0

    def event(self, stage, message, **fields):
        if stage == "router.classifier_fallback":
            self.fallbacks += 1


def score_result(
    case: dict[str, Any], decision: RouterDecision | None
) -> dict[str, Any]:
    expected = RouterDecision.model_validate(case["expected"])
    required = set(expected.domains)
    selected = set(effective_router_domains(decision)) if decision else set()
    return {
        "exact_match": decision == expected,
        "required_domains": len(required),
        "required_domains_found": len(required & selected),
        "critical_pass": decision == expected if case["critical"] else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = sorted(
        row["latency_ms"] for row in rows if row["latency_ms"] is not None
    )
    required = sum(row["required_domains"] for row in rows)
    found = sum(row["required_domains_found"] for row in rows)
    costs = [row["cost_usd"] for row in rows]
    return {
        "cases": len(rows),
        "errors": sum(row["error"] is not None for row in rows),
        "exact_agreement": sum(row["exact_match"] for row in rows) / len(rows)
        if rows
        else None,
        "required_domain_recall": found / required if required else None,
        "critical_failures": [
            row["id"] for row in rows if row["critical_pass"] is False
        ],
        "fallback_calls": sum(row["fallback_calls"] for row in rows),
        "fallback_frequency": sum(row["fallback_calls"] > 0 for row in rows) / len(rows)
        if rows
        else None,
        "mean_latency_ms": round(statistics.mean(latencies), 1) if latencies else None,
        "p50_latency_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_latency_ms": latencies[
            min(len(latencies) - 1, int((len(latencies) - 1) * 0.95))
        ]
        if latencies
        else None,
        "cost_usd": str(sum((Decimal(cost) for cost in costs), Decimal(0)))
        if all(cost is not None for cost in costs)
        else None,
    }


def evaluate(cases, personas, client=None, replay=None):
    rows = []
    for case in cases:
        ledger, trace = UsageLedger(), EvaluationTrace()
        decision, failure = None, None
        started = time.perf_counter()
        if replay is not None:
            saved = replay.get(case["id"])
            if saved is None:
                continue
        try:
            decision = (
                decision_from_answers(saved["raw_answers"])
                if replay is not None
                else client.classify(
                    case["query"],
                    personas[case["persona"]].snapshot,
                    usage_accumulator=ledger,
                    tracer=trace,
                )
            )
        except (RouterClientError, ValueError) as error:
            failure = type(error).__name__
        costs = [
            calculate_usage_record_cost_usd(record, quantum=Decimal("0.00000001"))
            for record in ledger.calls
        ]
        rows.append(
            {
                "id": case["id"],
                "persona": case["persona"],
                "query": case["query"],
                "expected": case["expected"],
                "decision": decision.model_dump(mode="json") if decision else None,
                "error": failure,
                "latency_ms": None
                if replay is not None
                else round((time.perf_counter() - started) * 1000, 1),
                "fallback_calls": trace.fallbacks,
                "returned_models": (
                    [saved["returned_model"]]
                    if replay is not None
                    else [record.returned_model for record in ledger.calls]
                ),
                "input_tokens": (
                    None
                    if replay is not None
                    else sum(record.prompt_tokens for record in ledger.calls)
                ),
                "output_tokens": (
                    None
                    if replay is not None
                    else sum(record.completion_tokens for record in ledger.calls)
                ),
                "cost_usd": (
                    None
                    if replay is not None
                    or not costs
                    or any(cost is None for cost in costs)
                    else str(sum(costs, Decimal(0)))
                ),
                **score_result(case, decision),
            }
        )
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine", choices=["all", "typesafe", "llm", "chain", "replay"], default="all"
    )
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "tests/data/domain_router_cases.json"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--critical-only", action="store_true")
    parser.add_argument("--ids", nargs="*")
    args = parser.parse_args(argv)
    cases = json.loads(args.cases.read_text())["cases"]
    if args.critical_only:
        cases = [case for case in cases if case["critical"]]
    if args.ids:
        cases = [case for case in cases if case["id"] in args.ids]
    if not cases:
        parser.error("No matching cases")
    engines = ["typesafe", "llm", "chain"] if args.engine == "all" else [args.engine]
    if (
        any(engine in {"typesafe", "chain"} for engine in engines)
        and not settings.typesafe_api_key
    ):
        parser.error(
            "TYPESAFE_API_KEY is required for live Jev evaluation; use --engine replay for offline inspection"
        )
    personas = {persona.name: persona for persona in load_personas(DEFAULT_USERS_DIR)}
    report = {
        "mode": args.engine,
        "requested_jev_model": (
            settings.typesafe_model
            if any(engine in {"typesafe", "chain"} for engine in engines)
            else None
        ),
        "engines": {},
    }
    llm, jev = None, None
    try:
        if any(engine in {"llm", "chain"} for engine in engines):
            llm = RouterClient(profile=settings.router_llm)
        if any(engine in {"typesafe", "chain"} for engine in engines):
            jev = TypeSafeRouterClient(
                settings.typesafe_api_key,
                model=settings.typesafe_model,
                base_url=settings.typesafe_base_url,
                request_timeout_seconds=settings.typesafe_router_request_timeout_seconds,
                max_retry_attempts=settings.typesafe_router_max_retry_attempts,
                retry_max_delay_seconds=settings.typesafe_router_retry_max_delay_seconds,
            )
        for engine in engines:
            replay = None
            if engine == "replay":
                path = ROOT / "tests/data/router_evals/20260917T162047Z.typesafe.jsonl"
                replay = {
                    f"historical-{i:03}": json.loads(line)
                    for i, line in enumerate(path.read_text().splitlines(), 1)
                }
                client = None
            else:
                client = {
                    "llm": llm,
                    "typesafe": jev,
                    "chain": RoutingChain(jev, lambda: llm),
                }[engine]
            rows = evaluate(cases, personas, client=client, replay=replay)
            report["engines"][engine] = {"summary": summarize(rows), "rows": rows}
        if args.engine == "all":
            summaries = {
                key: value["summary"] for key, value in report["engines"].items()
            }
            baseline_recall = summaries["llm"]["required_domain_recall"]
            chain_recall = summaries["chain"]["required_domain_recall"]
            report["acceptance_passed"] = not summaries["chain"][
                "critical_failures"
            ] and (
                baseline_recall is None
                or (chain_recall is not None and chain_recall >= baseline_recall)
            )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                {key: value["summary"] for key, value in report["engines"].items()},
                indent=2,
            )
        )
    finally:
        if jev:
            jev.close()
        if llm:
            llm.close()
    return 0 if report.get("acceptance_passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
