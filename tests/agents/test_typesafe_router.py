"""Tests for the TypeSafe (Jev) benchmark router.

Two things are worth testing here and both are pure — no API key, no network:

1. ``decision_from_answers``, which reassembles four independent probability
   signals into a :class:`RouterDecision`. That schema cross-validates its fields,
   so most of the function is reconciliation and every rung can be exercised
   directly.
2. That the Noul criteria are genuinely rendered from each persona's routing
   policy rather than being static text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.agent_api.app.router.prompt import (
    QueryComplexity,
    RouterDecision,
    RouterOutcome,
)
from agents.agent_api.app.router.typesafe import (
    COMPLEXITY_QUESTION,
    NAMED_MULTIPLE,
    NAMED_NONE,
    NAMED_PROVIDER_CONFIDENCE_FLOOR,
    NAMED_PROVIDER_QUESTION,
    NOUL_IN,
    NOUL_MAYBE,
    OUTCOME_CONFIDENCE_FLOOR,
    REQUEST_KIND_QUESTION,
    build_questions,
    build_state,
    build_typesafe_request,
    decision_from_answers,
    noul_question_id,
)
from scripts.eval_router import build_snapshot_from_fixture

USERS_DIR = Path(__file__).resolve().parents[2] / "tests" / "data" / "router_users"


def _snapshot(name: str):
    data = json.loads((USERS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return build_snapshot_from_fixture(data, name=name)


def _answers(
    *,
    todoist: float = 0.0,
    google_calendar: float = 0.0,
    kind: str = "service_request",
    confidence: float = 1.0,
    score: float = 0.0,
    named: str = NAMED_NONE,
    named_confidence: float = 1.0,
) -> dict:
    """Build a synthetic TypeSafe answers payload."""

    return {
        noul_question_id("todoist"): {"type": "noul", "noul": todoist},
        noul_question_id("google_calendar"): {"type": "noul", "noul": google_calendar},
        REQUEST_KIND_QUESTION: {
            "type": "choice",
            "choice": kind,
            "confidence": confidence,
            "probabilities": {kind: confidence},
        },
        NAMED_PROVIDER_QUESTION: {
            "type": "choice",
            "choice": named,
            "confidence": named_confidence,
            "probabilities": {named: named_confidence},
        },
        COMPLEXITY_QUESTION: {
            "type": "score",
            "score": score,
            "legend": {"0": "low", "1": "medium", "2": "high"},
            "probabilities": {"0": 1.0},
        },
    }


# ---------------------------------------------------------------------------
# Straightforward composition
# ---------------------------------------------------------------------------


def test_single_confident_domain_routes():
    decision = decision_from_answers(_answers(todoist=0.97, google_calendar=0.01))

    assert decision.outcome is RouterOutcome.ROUTED
    assert [domain.value for domain in decision.domains] == ["todoist"]
    assert decision.uncertain is False
    assert decision.candidate_domains == []


def test_both_domains_route_together():
    decision = decision_from_answers(_answers(todoist=0.95, google_calendar=0.91))

    assert [domain.value for domain in decision.domains] == [
        "todoist",
        "google_calendar",
    ]
    assert decision.uncertain is False


def test_conversation_carries_no_domains():
    decision = decision_from_answers(
        _answers(todoist=0.02, google_calendar=0.01, kind="conversation")
    )

    assert decision.outcome is RouterOutcome.CONVERSATION
    assert decision.domains == []
    assert decision.candidate_domains == []


def test_unsupported_provider_carries_no_domains():
    decision = decision_from_answers(
        _answers(todoist=0.05, google_calendar=0.02, kind="unsupported_provider")
    )

    assert decision.outcome is RouterOutcome.UNSUPPORTED_PROVIDER
    assert decision.domains == []


def test_non_routed_outcome_strips_confident_domains():
    """A high Noul cannot smuggle a domain into a non-routed outcome."""

    decision = decision_from_answers(
        _answers(todoist=0.99, google_calendar=0.0, kind="conversation")
    )

    assert decision.outcome is RouterOutcome.CONVERSATION
    assert decision.domains == []


# ---------------------------------------------------------------------------
# Uncertainty
# ---------------------------------------------------------------------------


def test_borderline_noul_makes_the_decision_uncertain():
    """A Noul between the two thresholds is real domain ambiguity."""

    borderline = (NOUL_MAYBE + NOUL_IN) / 2
    decision = decision_from_answers(
        _answers(todoist=0.93, google_calendar=borderline)
    )

    assert decision.uncertain is True
    assert [domain.value for domain in decision.domains] == ["todoist"]
    assert [domain.value for domain in decision.candidate_domains] == [
        "todoist",
        "google_calendar",
    ]


def test_low_outcome_confidence_makes_the_decision_uncertain():
    decision = decision_from_answers(
        _answers(
            todoist=0.88,
            google_calendar=0.01,
            confidence=OUTCOME_CONFIDENCE_FLOOR - 0.01,
        )
    )

    assert decision.uncertain is True
    assert decision.candidate_domains  # schema requires these when uncertain


def test_candidate_domains_always_cover_routed_domains():
    """The schema requires candidates ⊇ domains; a split read must still satisfy it."""

    decision = decision_from_answers(
        _answers(todoist=0.99, google_calendar=0.30, confidence=0.20)
    )

    assert set(decision.domains).issubset(set(decision.candidate_domains))


def test_unclear_service_becomes_ambiguous_and_uncertain():
    decision = decision_from_answers(
        _answers(todoist=0.40, google_calendar=0.35, kind="unclear_service")
    )

    assert decision.outcome is RouterOutcome.AMBIGUOUS
    assert decision.uncertain is True
    assert decision.domains == []
    assert len(decision.candidate_domains) == 2


# ---------------------------------------------------------------------------
# Explicitly named provider — a code rule, not a prose criterion
# ---------------------------------------------------------------------------


def test_named_provider_wins_over_a_higher_competing_noul():
    """The live failure: `whats on my google cal tmr` gave todoist 0.74, gcal 0.58."""

    decision = decision_from_answers(
        _answers(todoist=0.74, google_calendar=0.58, named="google_calendar")
    )

    assert [domain.value for domain in decision.domains] == ["google_calendar"]
    assert decision.uncertain is False


def test_named_provider_routes_even_when_its_own_noul_is_low():
    """Naming is a fact about the text, so it does not need the Noul's agreement."""

    decision = decision_from_answers(
        _answers(todoist=0.91, google_calendar=0.12, named="google_calendar")
    )

    assert [domain.value for domain in decision.domains] == ["google_calendar"]


def test_low_confidence_naming_does_not_override():
    """Below the floor the answer is not decisive, so the Nouls still decide."""

    decision = decision_from_answers(
        _answers(
            todoist=0.91,
            google_calendar=0.12,
            named="google_calendar",
            named_confidence=NAMED_PROVIDER_CONFIDENCE_FLOOR - 0.01,
        )
    )

    assert [domain.value for domain in decision.domains] == ["todoist"]


def test_naming_nothing_leaves_multi_domain_routing_intact():
    """`…my todos and whats booked on my cal` names no service; both may route."""

    decision = decision_from_answers(
        _answers(todoist=0.95, google_calendar=0.88, named=NAMED_NONE)
    )

    assert [domain.value for domain in decision.domains] == [
        "todoist",
        "google_calendar",
    ]


def test_naming_several_services_does_not_narrow_the_route():
    """Rule 11: a request touching two named domains keeps both."""

    decision = decision_from_answers(
        _answers(todoist=0.93, google_calendar=0.90, named=NAMED_MULTIPLE)
    )

    assert [domain.value for domain in decision.domains] == [
        "todoist",
        "google_calendar",
    ]


def test_named_provider_clears_a_borderline_noul_on_the_discarded_domain():
    """Once the route is settled, a mid-band Noul must not re-raise uncertainty."""

    borderline = (NOUL_MAYBE + NOUL_IN) / 2
    decision = decision_from_answers(
        _answers(todoist=borderline, google_calendar=0.95, named="google_calendar")
    )

    assert decision.uncertain is False
    assert decision.candidate_domains == []


def test_named_provider_does_not_resurrect_a_non_routed_outcome():
    """A named service cannot turn an unsupported-provider request into a route."""

    decision = decision_from_answers(
        _answers(todoist=0.4, google_calendar=0.9, named="google_calendar", kind="conversation")
    )

    assert decision.outcome is RouterOutcome.CONVERSATION
    assert decision.domains == []


# ---------------------------------------------------------------------------
# Reconciliation rungs — each exists because a validator would otherwise raise
# ---------------------------------------------------------------------------


def test_routed_with_no_confident_domain_falls_back_to_the_best_candidate():
    decision = decision_from_answers(_answers(todoist=0.45, google_calendar=0.10))

    assert decision.outcome is RouterOutcome.ROUTED
    assert [domain.value for domain in decision.domains] == ["todoist"]


def test_routed_with_no_plausible_domain_degrades_to_conversation():
    """`routed` requires a domain; with nothing plausible it cannot stay routed."""

    decision = decision_from_answers(_answers(todoist=0.03, google_calendar=0.02))

    assert decision.outcome is RouterOutcome.CONVERSATION
    assert decision.domains == []
    assert decision.candidate_domains == []


def test_ambiguous_without_candidates_degrades_to_conversation():
    """`ambiguous` requires candidates, and candidates require a plausible domain."""

    decision = decision_from_answers(
        _answers(todoist=0.01, google_calendar=0.01, kind="unclear_service")
    )

    assert decision.outcome is RouterOutcome.CONVERSATION
    assert decision.uncertain is False
    assert decision.candidate_domains == []


def test_unknown_choice_option_degrades_safely():
    """An unrecognised option must not raise; it falls back to conversation."""

    decision = decision_from_answers(
        _answers(todoist=0.9, google_calendar=0.0, kind="something_new")
    )

    assert decision.outcome is RouterOutcome.CONVERSATION
    assert decision.domains == []


def test_missing_answers_do_not_raise():
    """A truncated payload composes to a safe decision rather than exploding."""

    decision = decision_from_answers({})

    assert isinstance(decision, RouterDecision)
    assert decision.outcome is RouterOutcome.CONVERSATION


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, QueryComplexity.LOW),
        (0.6, QueryComplexity.LOW),
        (0.7, QueryComplexity.MEDIUM),
        (1.3, QueryComplexity.MEDIUM),
        (1.4, QueryComplexity.HIGH),
        (2.0, QueryComplexity.HIGH),
    ],
)
def test_complexity_score_buckets_into_three_levels(score, expected):
    decision = decision_from_answers(_answers(todoist=0.9, score=score))

    assert decision.complexity is expected


# ---------------------------------------------------------------------------
# Per-persona rendering
# ---------------------------------------------------------------------------


def test_explicit_only_persona_gets_a_stricter_calendar_noul():
    """jerry.json is explicit-only; nadia.json is not. The criteria must differ."""

    jerry = build_questions(_snapshot("jerry"))[noul_question_id("google_calendar")]
    nadia = build_questions(_snapshot("nadia"))[noul_question_id("google_calendar")]

    assert "explicit_only" in jerry["criteria"]["true"]
    assert "generic_scheduling_is_not_enough" in jerry["criteria"]["false"]
    assert "explicit_only" not in nadia["criteria"]["true"]
    assert jerry["criteria"] != nadia["criteria"]


def test_todoist_noul_reflects_who_owns_events():
    """jerry routes events to Todoist; nadia routes them to Google Calendar."""

    jerry = build_questions(_snapshot("jerry"))[noul_question_id("todoist")]
    nadia = build_questions(_snapshot("nadia"))[noul_question_id("todoist")]

    assert "events and meetings" in jerry["criteria"]["true"]["this_user_routes_here"]
    assert "events and meetings" not in nadia["criteria"]["true"]["this_user_routes_here"]


def test_domain_nouls_do_not_restate_minimality():
    """Minimality lives in `named_provider` + code, not in each Noul's prose.

    Stating it per-Noul was unreliable — two independent questions each had to
    rediscover whether a service was named. Keeping it out prevents that
    regression from creeping back in.
    """

    questions = build_questions(_snapshot("jerry"))

    for domain in ("todoist", "google_calendar"):
        criteria = questions[noul_question_id(domain)]["criteria"]
        assert "not_for" not in criteria["true"], domain


def test_every_domain_noul_decouples_need_from_connection():
    """Rule 12: a disconnected domain the request needs must still be reported."""

    for name in ("jerry", "marcus", "phoebe"):
        questions = build_questions(_snapshot(name))
        for domain in ("todoist", "google_calendar"):
            criterion = questions[noul_question_id(domain)]["criteria"]["true"]
            assert "ignore_availability" in criterion, (name, domain)


def test_state_reports_connection_status_per_persona():
    """phoebe has only Google Calendar connected; the state must say so."""

    connected = {
        domain["key"]: domain["connected"]
        for domain in build_state("hi", _snapshot("phoebe"))["available_domains"]
    }

    assert connected == {"todoist": False, "google_calendar": True}


def test_request_carries_state_model_and_every_question():
    request = build_typesafe_request("what do i have tomorrow?", _snapshot("jerry"))

    assert request["state"]["user_request"] == "what do i have tomorrow?"
    assert request["model"] == "jev-latest"
    assert set(request["questions"]) == {
        noul_question_id("todoist"),
        noul_question_id("google_calendar"),
        REQUEST_KIND_QUESTION,
        NAMED_PROVIDER_QUESTION,
        COMPLEXITY_QUESTION,
    }


def test_named_provider_question_offers_every_domain_plus_none_and_multiple():
    question = build_questions(_snapshot("jerry"))[NAMED_PROVIDER_QUESTION]

    assert set(question["criteria"]) == {
        NAMED_NONE,
        "todoist",
        "google_calendar",
        NAMED_MULTIPLE,
    }


def test_unsupported_provider_excludes_places_and_activities():
    """Without this guard, any unrecognised proper noun reads as a provider.

    "opening hours of Truvato near my house" scored 0.58 on
    ``unsupported_provider`` purely because the criterion had no negative case —
    a misspelled restaurant looked like an unlisted service.
    """

    criteria = build_questions(_snapshot("jerry"))[REQUEST_KIND_QUESTION]["criteria"]

    assert "not_for" in criteria["unsupported_provider"]


def test_named_provider_counts_a_name_carried_by_reply_context():
    """A correction like `today*` inherits the provider its quoted request named.

    "Judge only the words used" alone made the router answer `none` at 0.96 on a
    reply to `what's on my google cal for tmr`, sending a calendar read to
    Todoist.
    """

    question = build_questions(_snapshot("jerry"))[NAMED_PROVIDER_QUESTION]

    assert "Reply context" in question["instructions"]["focus"]
