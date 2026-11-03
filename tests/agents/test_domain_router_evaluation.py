"""Labels and metrics for the production router comparison are inspectable."""

import json
from pathlib import Path
from unittest.mock import Mock

from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.router.runtime import RoutingChain
from scripts.eval_domain_router import evaluate, score_result, summarize
from scripts.eval_router import (
    build_current_snapshot_from_fixture,
    load_personas,
    DEFAULT_USERS_DIR,
)
from tests.agents.test_typesafe_runtime import fake


def test_current_fixture_does_not_require_legacy_preferences():
    snapshot = build_current_snapshot_from_fixture(
        {
            "display_name": "Current user",
            "active_providers": ["todoist"],
            "custom_instructions": "Tasks go to Todoist.",
        }
    )
    assert snapshot.schema_version == 2
    assert snapshot.custom_instructions == "Tasks go to Todoist."


def test_labels_are_valid_and_historical_queries_are_preserved():
    root = Path(__file__).resolve().parents[2]
    data = json.loads((root / "tests/data/domain_router_cases.json").read_text())
    rows = [
        json.loads(line) for line in (root / data["source"]).read_text().splitlines()
    ]
    cases = data["cases"]
    assert len(cases) == 113
    assert len({case["id"] for case in cases}) == len(cases)
    assert [case["query"] for case in cases[:98]] == [row["query"] for row in rows]
    for case in cases:
        RouterDecision.model_validate(case["expected"])
    assert all(case["critical"] for case in cases[98:])


def test_comparison_counts_actual_chain_fallbacks():
    case = {
        "id": "example",
        "query": "tasks",
        "persona": "jerry",
        "critical": True,
        "expected": {
            "outcome": "routed",
            "domains": ["todoist"],
            "uncertain": False,
            "candidate_domains": [],
            "complexity": "low",
        },
    }
    expected = RouterDecision.model_validate(case["expected"])
    uncertain = expected.model_copy(
        update={"uncertain": True, "candidate_domains": ["todoist"]}
    )
    chain = RoutingChain(fake(uncertain), Mock(return_value=fake(expected)))
    personas = {persona.name: persona for persona in load_personas(DEFAULT_USERS_DIR)}
    rows = evaluate([case], personas, client=chain)
    summary = summarize(rows)
    assert summary["fallback_calls"] == 1
    assert summary["fallback_frequency"] == 1
    assert summary["exact_agreement"] == 1
    assert summary["required_domain_recall"] == 1
    assert summary["critical_failures"] == []
    assert summary["cost_usd"] is None  # Never report missing usage as free.


def test_required_recall_uses_safe_candidates_without_claiming_exact_match():
    case = {
        "critical": True,
        "expected": {
            "outcome": "routed",
            "domains": ["todoist"],
            "uncertain": False,
            "candidate_domains": [],
            "complexity": "low",
        },
    }
    decision = RouterDecision(
        outcome="ambiguous",
        domains=[],
        uncertain=True,
        candidate_domains=["todoist", "google_calendar"],
        complexity="low",
    )
    score = score_result(case, decision)
    assert score["required_domains_found"] == 1
    assert score["required_domains"] == 1
    assert not score["exact_match"]
    assert not score["critical_pass"]


def test_replay_cli_does_not_claim_current_model_or_token_measurements(tmp_path):
    from scripts.eval_domain_router import main

    out = tmp_path / "replay.json"
    assert (
        main(["--engine", "replay", "--ids", "historical-001", "--out", str(out)]) == 0
    )
    report = json.loads(out.read_text())
    assert report["requested_jev_model"] is None
    row = report["engines"]["replay"]["rows"][0]
    assert row["returned_models"] == ["jev-1.13.0"]
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cost_usd"] is None
