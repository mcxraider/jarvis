#!/usr/bin/env python3
"""Evaluate router decisions across persona and query fixtures.

This harness intentionally bypasses Supabase and constructs the router's real
input directly: a query string plus a RuntimeContextSnapshot. The live path uses
RouterClient, while tests can inject a fake client and exercise everything else
without making an API call.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence

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

from agents.agent_api.app.router.client import RouterClient, RouterClientError
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
    elapsed_ms: int
    error: bool = False


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


def load_queries(
    queries_file: Path,
    *,
    query_filters: Optional[Sequence[str]] = None,
) -> List[tuple[int, str]]:
    module_globals = runpy.run_path(str(queries_file))
    raw_queries = module_globals.get("ROUTER_QUERIES")
    if not isinstance(raw_queries, list) or not all(isinstance(query, str) for query in raw_queries):
        raise ValueError(f"{queries_file} must define ROUTER_QUERIES as a list of strings")

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


def apply_guardrails(query: str, snapshot: RuntimeContextSnapshot, decision: RouterDecision) -> RouterDecision:
    selector = RouterToolSelector(
        router_client=_GuardrailClient(),
        snapshot=snapshot,
        tracer=NULL_TRACE,
    )
    return selector._apply_routing_guardrails(query, decision)


def evaluate_pair(
    persona: Persona,
    query_index: int,
    query: str,
    router_client: Any,
) -> EvalResult:
    messages = build_router_messages(query, persona.snapshot)
    started = time.perf_counter()
    try:
        decision = router_client.classify(query, persona.snapshot)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        adjusted = apply_guardrails(query, persona.snapshot, decision)
        raw_payload = _model_payload(decision)
        adjusted_payload = _model_payload(adjusted)
        return EvalResult(
            persona=persona,
            query_index=query_index,
            query=query,
            system_prompt=messages[0]["content"],
            user_prompt=messages[1]["content"],
            raw_response=raw_payload,
            adjusted_response=adjusted_payload if adjusted_payload != raw_payload else None,
            elapsed_ms=elapsed_ms,
        )
    except RouterClientError as error:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return EvalResult(
            persona=persona,
            query_index=query_index,
            query=query,
            system_prompt=messages[0]["content"],
            user_prompt=messages[1]["content"],
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
                f"Task provider: {routing.task_provider} - Event provider: {routing.event_provider} - calendar_usage: {routing.calendar_usage} - Active: {active}",
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
            lines.extend([f"**Elapsed:** {result.elapsed_ms} ms", ""])

    return "\n".join(lines).rstrip() + "\n"


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
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if not (os.getenv("ROUTER_API_KEY") or os.getenv("DEEPSEEK_API_KEY")):
        raise SystemExit("ROUTER_API_KEY (or DEEPSEEK_API_KEY) is required.")

    verbose = not args.quiet
    if verbose:
        tqdm.write("Loading router eval fixtures...")
        tqdm.write(f"Users dir: {args.users_dir}")
        tqdm.write(f"Queries file: {args.queries_file}")
    personas = load_personas(args.users_dir, user_filters=args.users)
    queries = load_queries(args.queries_file, query_filters=args.queries)
    router_client = RouterClient(tracer=NULL_TRACE)
    if verbose:
        tqdm.write(
            f"Loaded {len(personas)} persona(s), {len(queries)} query/queries, "
            f"{len(personas) * len(queries)} eval pair(s)."
        )
        tqdm.write(f"Router model: {getattr(router_client, 'model', 'unknown')}")
    run_at = _utc_now()
    results = run_grid(personas, queries, router_client, verbose=verbose)
    error_count = sum(1 for result in results if result.error)
    adjusted_count = sum(1 for result in results if result.adjusted_response is not None)
    if verbose:
        tqdm.write(
            f"Evaluation complete: {len(results)} result(s), "
            f"{error_count} error(s), {adjusted_count} guardrail adjustment(s)."
        )
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
