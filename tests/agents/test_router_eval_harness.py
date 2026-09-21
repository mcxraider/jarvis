"""Smoke tests for the standalone router evaluation harness.

The live harness calls DeepSeek through RouterClient. These tests inject a fake
client so normal pytest runs validate fixture loading and markdown formatting
without making network calls.
"""

import os
from datetime import datetime, timezone

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

import json
from decimal import Decimal

from agents.agent_api.app.llm.chat import UsageRecord
from agents.agent_api.app.llm.provider import LLMProvider
from agents.agent_api.app.router.prompt import RouterDecision
from scripts.eval_router import (
    DEFAULT_USERS_DIR,
    append_results,
    format_markdown,
    load_personas,
    load_queries,
    result_row,
    run_grid,
    select_queries,
    summarize,
    write_persona_reports,
)


class FakeRouterClient:
    """Stands in for RouterClient, including its usage-accumulator contract."""

    model = "deepseek-v4-flash"
    reasoning_effort = "off"
    base_url = "https://fake.invalid/v1"
    request_timeout_seconds = 5.0
    max_retry_attempts = 2

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def classify(self, query, snapshot, usage_accumulator=None):
        self.calls += 1
        if usage_accumulator is not None:
            usage_accumulator.add(
                UsageRecord(
                    provider=LLMProvider.DEEPSEEK,
                    requested_model=self.model,
                    returned_model=self.model,
                    prompt_tokens=1000,
                    completion_tokens=40,
                    cached_read_tokens=0,
                    cache_write_tokens=0,
                    reasoning_tokens=0,
                    request_input_tokens=1000,
                )
            )
        return self.decision


def test_router_eval_loads_fixtures_and_formats_markdown_without_api_call(tmp_path):
    all_personas = load_personas(DEFAULT_USERS_DIR)
    assert {persona.name for persona in all_personas} == {
        "avery",
        "jerry",
        "marcus",
        "nadia",
        "phoebe",
        "user-1",
        "zac",
    }

    personas = load_personas(DEFAULT_USERS_DIR, user_filters=["jerry"])
    queries_file = tmp_path / "router_queries.py"
    queries_file.write_text('ROUTER_QUERIES = ["put in my cal"]\n', encoding="utf-8")
    queries = load_queries(queries_file, query_filters=["put in my cal"])
    client = FakeRouterClient(RouterDecision(outcome="routed", domains=["google_calendar"], uncertain=False, candidate_domains=[], complexity="low"))

    results = run_grid(personas, queries, client)
    markdown = format_markdown(
        run_at=datetime(2026, 7, 7, 14, 33, 12, tzinfo=timezone.utc),
        model=client.model,
        personas=personas,
        queries=queries,
        results=results,
    )
    report_paths = write_persona_reports(
        run_at=datetime(2026, 7, 7, 14, 33, 12, tzinfo=timezone.utc),
        model=client.model,
        personas=personas,
        queries=queries,
        results=results,
        out_dir=tmp_path / "router_evals",
    )

    assert client.calls == 1
    assert personas[0].snapshot.preferences.routing.calendar_usage == "explicit_only"
    assert personas[0].snapshot.active_providers() == {"todoist", "google_calendar"}
    assert results[0].raw_response["domains"] == ["google_calendar"]
    assert "reasoning" not in results[0].raw_response
    assert results[0].adjusted_response["domains"] == ["todoist"]
    assert "# Router evaluation - 2026-07-07T14:33:12Z" in markdown
    assert "Reminder provider: todoist" in markdown
    assert "Time-related provider: todoist" in markdown
    assert "Explicit calendar provider: todoist" in markdown
    assert (
        "**Model:** deepseek-v4-flash - **Users:** 1 - **Queries:** 1 - **Pairs:** 1"
        in markdown
    )
    assert "**Prompt (system):**" in markdown
    assert "**Prompt (user):**" in markdown
    assert "**Response (raw RouterDecision):**" in markdown
    assert "**Response (after guardrails):**" in markdown
    assert "put in my cal" in markdown
    assert report_paths == [tmp_path / "router_evals" / "jerry" / "20260707T143312Z.md"]
    assert report_paths[0].read_text(encoding="utf-8") == markdown


def test_router_eval_records_latency_cost_and_excludes_fast_path_from_summary(tmp_path):
    run_at = datetime(2026, 7, 7, 14, 33, 12, tzinfo=timezone.utc)
    personas = load_personas(DEFAULT_USERS_DIR, user_filters=["user-1"])
    # A filled-in PROMPTS list is numbered and filtered the same way the fixture
    # file is; "hello" is answered by the deterministic fast path, so production
    # never sends it to the LLM, while "put in my cal" is a real classification.
    queries = select_queries(["put in my cal", "hello"])
    assert queries == [(1, "put in my cal"), (2, "hello")]
    assert select_queries(["a", "b", "c"], query_filters=["2"]) == [(2, "b")]
    client = FakeRouterClient(
        RouterDecision(
            outcome="routed",
            domains=["google_calendar"],
            uncertain=False,
            candidate_domains=[],
            complexity="low",
        )
    )

    results = run_grid(personas, queries, client)
    rows = [result_row(result, run_at=run_at) for result in results]
    summary = summarize(
        run_at=run_at,
        router_client=client,
        tracing_enabled=False,
        personas=personas,
        queries=queries,
        results=results,
    )
    out_dir = tmp_path / "router_evals"
    append_results(results, out_dir, run_at=run_at)
    later = datetime(2026, 7, 7, 15, 0, 0, tzinfo=timezone.utc)
    results_path = append_results(results, out_dir, run_at=later)

    # The eval always calls the LLM so every row carries real I/O; the fast-path
    # flag is what keeps production-skipped queries out of the aggregates.
    assert client.calls == 2
    assert [row["fast_path_hit"] for row in rows] == [False, True]
    assert rows[1]["fast_path_decision"]["outcome"] == "conversation"

    row = rows[0]
    # The prompt itself is intentionally not recorded, only its hash.
    assert "system_prompt" not in row
    assert row["user_prompt"] == "User request:\nput in my cal"
    assert len(row["system_prompt_sha256"]) == 64
    assert row["run_at"] == "2026-07-07T14:33:12Z"
    assert row["decision"]["domains"] == ["google_calendar"]
    assert row["decision_adjusted"]["domains"] == ["todoist"]
    assert row["latency_ms"] >= 0.0
    assert row["prompt_tokens"] == 1000
    assert row["completion_tokens"] == 40
    assert Decimal(row["cost_usd"]) > 0
    assert row["error"] is None

    assert summary["pairs"] == 2
    assert summary["fast_path_hits"] == 1
    assert summary["llm_calls"] == 1
    assert summary["errors"] == 0
    assert summary["requested_model"] == "deepseek-v4-flash"
    assert summary["provider"] is None  # the fake exposes no provider profile
    assert summary["tokens"] == {
        "prompt": 1000,
        "cached_read": 0,
        "completion": 40,
        "reasoning": 0,
    }
    assert Decimal(summary["total_cost_usd"]) == Decimal(row["cost_usd"])
    assert summary["latency_ms"]["p50"] == row["latency_ms"]

    # Both runs live in one array; the second append must not clobber the first.
    written = json.loads(results_path.read_text(encoding="utf-8"))
    assert written[:2] == rows
    assert len(written) == 4
    assert [entry["run_at"] for entry in written[2:]] == ["2026-07-07T15:00:00Z"] * 2
