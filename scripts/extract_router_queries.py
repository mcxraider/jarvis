#!/usr/bin/env python3
"""Pull real user queries out of LangSmith's ``domain_router.classify.openai`` runs.

Searches the project by run *name*, not by trace-tree position, so it keeps
working when the LangGraph topology or root run naming changes. Traces that
never invoked the router simply do not match.

Output is a Python module defining ``ROUTER_QUERIES``, which is exactly what
``scripts/eval_router.py --queries-file`` already consumes:

    python3 scripts/extract_router_queries.py --start-time 2026-09-01
    python3 scripts/eval_router.py --queries-file tests/data/router_queries_langsmith.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(ROOT / ".env")

RUN_NAME = "domain_router.classify.openai"
QUERY_PREFIX = "User request:\n"
DEFAULT_OUT = ROOT / "tests" / "data" / "router_queries_langsmith.py"


def extract_query(inputs: Any) -> Optional[str]:
    """Return the user query from a router run's ``inputs``, or None."""
    if not isinstance(inputs, dict):
        return None
    messages = inputs.get("messages") or inputs.get("input") or []
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            return None
        return content.removeprefix(QUERY_PREFIX).strip() or None
    return None


def _as_datetime(value: Optional[date]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def fetch_queries(
    project: str,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], int, int]:
    """Return (queries in run order, runs seen, runs whose query failed to parse)."""
    from langsmith import Client

    client = Client()
    runs: Iterable[Any] = client.list_runs(
        project_name=project,
        run_type="llm",
        filter=f'eq(name, "{RUN_NAME}")',
        start_time=_as_datetime(start),
        limit=limit,
    )

    end_at = _as_datetime(end)
    queries: list[str] = []
    seen = 0
    unparsed = 0
    for run in runs:
        # Defensive: older SDKs ignore `filter`, so re-check the name locally.
        if run.name != RUN_NAME:
            continue
        if end_at is not None and run.start_time is not None:
            started = run.start_time
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started >= end_at:
                continue
        seen += 1
        query = extract_query(run.inputs)
        if query is None:
            unparsed += 1
            continue
        queries.append(query)
    return queries, seen, unparsed


def render_module(queries: list[str], header: str) -> str:
    lines = [
        '"""Router queries extracted from LangSmith. Generated - do not hand-edit."""',
        "",
        f"# {header}",
        "ROUTER_QUERIES = [",
    ]
    lines += [f"    {json.dumps(q, ensure_ascii=False)}," for q in queries]
    lines += ["]", ""]
    return "\n".join(lines)


def _selfcheck() -> None:
    assert extract_query(
        {
            "messages": [
                {"role": "system", "content": "you are a router"},
                {"role": "user", "content": "User request:\n15th bbq with gang p1"},
            ]
        }
    ) == "15th bbq with gang p1"
    assert extract_query(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "User request:\nhi"}]}
            ]
        }
    ) == "hi"
    assert extract_query({"messages": [{"role": "system", "content": "x"}]}) is None
    assert extract_query({"messages": []}) is None
    assert extract_query(None) is None
    assert extract_query({"messages": [{"role": "user", "content": "User request:\n"}]}) is None
    assert (
        extract_query(
            {"messages": [{"role": "user", "content": "User request:\nline one\nline two"}]}
        )
        == "line one\nline two"
    )
    # Last user message wins.
    assert extract_query(
        {
            "messages": [
                {"role": "user", "content": "User request:\nold"},
                {"role": "user", "content": "User request:\nnew"},
            ]
        }
    ) == "new"
    assert render_module(['say "hi"'], "h") .endswith('ROUTER_QUERIES = [\n    "say \\"hi\\"",\n]\n')
    print("selfcheck ok")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.getenv("LANGSMITH_PROJECT", "jarvis"))
    parser.add_argument("--start-time", type=date.fromisoformat, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-time", type=date.fromisoformat, help="YYYY-MM-DD (exclusive)")
    parser.add_argument("--limit", type=int, help="Max runs to scan.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--selfcheck", action="store_true", help="Run offline parser asserts.")
    args = parser.parse_args()

    if args.selfcheck:
        _selfcheck()
        return 0

    queries, seen, unparsed = fetch_queries(
        args.project, start=args.start_time, end=args.end_time, limit=args.limit
    )
    unique = list(dict.fromkeys(queries))

    window = f"{args.start_time or 'all'}..{args.end_time or 'now'}"
    header = f"project={args.project}  runs={seen}  unique={len(unique)}  window={window}"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_module(unique, header), encoding="utf-8")

    print(f"{seen} router runs -> {len(unique)} unique queries -> {args.out}", file=sys.stderr)
    if unparsed:
        print(f"warning: {unparsed} run(s) had no parseable user query", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
