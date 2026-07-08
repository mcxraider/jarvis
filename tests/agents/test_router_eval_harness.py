"""Smoke tests for the standalone router evaluation harness.

The live harness calls DeepSeek through RouterClient. These tests inject a fake
client so normal pytest runs validate fixture loading and markdown formatting
without making network calls.
"""

import os
from datetime import datetime, timezone

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from agents.agent_api.app.router.prompt import RouterDecision
from scripts.eval_router import (
    DEFAULT_USERS_DIR,
    format_markdown,
    load_personas,
    load_queries,
    run_grid,
    write_persona_reports,
)


class FakeRouterClient:
    model = "fake-router"

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    def classify(self, query, snapshot):
        self.calls += 1
        return self.decision


def test_router_eval_loads_fixtures_and_formats_markdown_without_api_call(tmp_path):
    all_personas = load_personas(DEFAULT_USERS_DIR)
    assert {persona.name for persona in all_personas} == {
        "avery",
        "jerry",
        "marcus",
        "nadia",
        "phoebe",
        "zac",
    }

    personas = load_personas(DEFAULT_USERS_DIR, user_filters=["jerry"])
    queries_file = tmp_path / "router_queries.py"
    queries_file.write_text('ROUTER_QUERIES = ["put in my cal"]\n', encoding="utf-8")
    queries = load_queries(queries_file, query_filters=["put in my cal"])
    client = FakeRouterClient(RouterDecision(domains=["google_calendar"], reasoning="raw cal"))

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
    assert results[0].adjusted_response["domains"] == ["todoist"]
    assert "# Router evaluation - 2026-07-07T14:33:12Z" in markdown
    assert "**Model:** fake-router - **Users:** 1 - **Queries:** 1 - **Pairs:** 1" in markdown
    assert "**Prompt (system):**" in markdown
    assert "**Prompt (user):**" in markdown
    assert "**Response (raw RouterDecision):**" in markdown
    assert "**Response (after guardrails):**" in markdown
    assert "put in my cal" in markdown
    assert report_paths == [tmp_path / "router_evals" / "jerry" / "20260707T143312Z.md"]
    assert report_paths[0].read_text(encoding="utf-8") == markdown
