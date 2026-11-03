"""Production Jev transport, fallback, accounting, and cancellation contracts."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agents.agent_api.app.config import load_settings
from agents.agent_api.app.graph.nodes.orchestrator import UsageSummary
from agents.agent_api.app.llm.chat import UsageLedger
from agents.agent_api.app.pricing import calculate_usage_record_cost_usd
from agents.agent_api.app.router.client import RouterClientError
from agents.agent_api.app.router.prompt import RouterDecision
from agents.agent_api.app.router.runtime import RoutingChain
from agents.agent_api.app.router.typesafe import (
    TypeSafeRouterClient,
    decision_from_answers,
)
from tests.agents.runtime_helpers import make_snapshot
from tests.agents.test_router_selector import _registry, _names, _ALL_TOOLS
from tests.agents.test_typesafe_router import _answers
from agents.agent_api.app.tools.selectors.router import RouterToolSelector


def body():
    return {
        "model": "jev-1.14.0",
        "answers": _answers(todoist=0.98),
        "usage": {"input_tokens": 1000, "output_tokens": 40},
    }


def client_for(handler):
    transport = httpx.MockTransport(handler)
    return TypeSafeRouterClient(
        "test-key",
        client=httpx.Client(base_url="https://api.typesafe.ai/v1", transport=transport),
        async_client=httpx.AsyncClient(
            base_url="https://api.typesafe.ai/v1", transport=transport
        ),
        retry_max_delay_seconds=0.001,
    )


@pytest.fixture
def client_factory():
    clients = []

    def make(handler):
        client = client_for(handler)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client._client.close()
        asyncio.run(client._async_client.aclose())


def fake(decision=None, error=None):
    client = Mock()
    client.classify = Mock(return_value=decision, side_effect=error)
    client.async_classify = AsyncMock(return_value=decision, side_effect=error)
    return client


def error():
    return RouterClientError({"source": "router", "type": "timeout"})


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize(
    "primary_result,fallback_result,expected",
    [
        ("certain", "certain", "primary"),
        ("uncertain", "certain", "fallback"),
        ("error", "certain", "fallback"),
        ("unavailable", "certain", "fallback"),
        ("uncertain", "error", "primary"),
        ("error", "error", "error"),
        ("unavailable", "error", "error"),
        ("uncertain", "uncertain", "fallback"),
    ],
)
def test_fallback_policy_and_selector_cache(
    async_call, primary_result, fallback_result, expected
):
    primary_decision = RouterDecision(
        outcome="routed",
        domains=["todoist"],
        uncertain=primary_result == "uncertain",
        candidate_domains=["todoist", "google_calendar"]
        if primary_result == "uncertain"
        else [],
        complexity="low",
    )
    fallback_decision = RouterDecision(
        outcome="routed",
        domains=["google_calendar"],
        uncertain=fallback_result == "uncertain",
        candidate_domains=["google_calendar"] if fallback_result == "uncertain" else [],
        complexity="medium",
    )
    primary = (
        None
        if primary_result == "unavailable"
        else fake(primary_decision, error() if primary_result == "error" else None)
    )
    fallback = fake(fallback_decision, error() if fallback_result == "error" else None)
    factory = Mock(return_value=fallback)
    usage = UsageSummary()
    chain = RoutingChain(primary, factory, usage_accumulator=usage)
    selector = RouterToolSelector(chain, make_snapshot())

    async def run():
        first = await selector.async_select_schemas("tasks", _registry())
        second = await selector.async_select_schemas("tasks", _registry())
        return first, second

    first, second = (
        asyncio.run(run())
        if async_call
        else (
            selector.select_schemas("tasks", _registry()),
            selector.select_schemas("tasks", _registry()),
        )
    )
    assert first == second
    if expected == "error":
        assert selector.decision is None
        assert _names(first) == _ALL_TOOLS
    else:
        assert selector.decision is (
            primary_decision if expected == "primary" else fallback_decision
        )
    assert factory.call_count == (0 if primary_result == "certain" else 1)
    if primary:
        method = primary.async_classify if async_call else primary.classify
        assert method.call_count == 1
        assert method.call_args.kwargs["usage_accumulator"] is usage


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("ledger_type", [UsageSummary, UsageLedger])
def test_transport_endpoint_accounting_and_pricing(
    client_factory, async_call, ledger_type
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body())

    client = client_factory(handle)
    usage = ledger_type()
    result = (
        asyncio.run(
            client.async_classify("tasks", make_snapshot(), usage_accumulator=usage)
        )
        if async_call
        else client.classify("tasks", make_snapshot(), usage_accumulator=usage)
    )
    assert result.domains == ["todoist"]
    assert str(requests[0].url) == "https://api.typesafe.ai/v1/systemone"
    records = usage.records if isinstance(usage, UsageSummary) else usage.calls
    assert len(records) == 1
    assert records[0].prompt_tokens == 1000
    assert calculate_usage_record_cost_usd(
        records[0], quantum=Decimal("0.00000001")
    ) == Decimal("0.00004200")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.update(answers=[]),
        lambda b: b["answers"].pop("request_kind"),
        lambda b: b["answers"]["request_kind"].update(choice="new-option"),
        lambda b: b["answers"]["request_kind"].update(confidence=True),
        lambda b: b["answers"]["needs_todoist"].update(noul=float("nan")),
        lambda b: b["answers"]["needs_todoist"].update(noul=float("inf")),
        lambda b: b["answers"]["needs_todoist"].update(noul=-0.1),
        lambda b: b["answers"]["needs_todoist"].update(type="choice"),
        lambda b: b["answers"]["complexity"].update(score=3),
        lambda b: b["answers"]["request_kind"].update(
            probabilities={"service_request": 1.0}
        ),
    ],
)
def test_invalid_answers_reject_instead_of_becoming_conversation(mutation):
    value = body()
    mutation(value)
    with pytest.raises(ValueError):
        decision_from_answers(value["answers"])


def test_rejected_decision_still_records_usage_once(client_factory):
    value = body()
    value["answers"].pop("request_kind")
    client = client_factory(lambda _: httpx.Response(200, json=value))
    usage = UsageSummary()
    with pytest.raises(RouterClientError):
        client.classify("tasks", make_snapshot(), usage_accumulator=usage)
    assert len(usage.records) == 1


@pytest.mark.parametrize(
    "response",
    [
        [],
        None,
        "text",
        {"usage": {"input_tokens": -1}},
        {"usage": {"input_tokens": True}},
    ],
)
def test_invalid_body_is_router_error(client_factory, response):
    client = client_factory(lambda _: httpx.Response(200, json=response))
    with pytest.raises(RouterClientError):
        client.classify("tasks", make_snapshot())


@pytest.mark.parametrize(
    "status,attempts", [(401, 1), (422, 1), (429, 2), (529, 2), (503, 2)]
)
@pytest.mark.parametrize("async_call", [False, True])
def test_retry_policy(client_factory, status, attempts, async_call):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, headers={"Retry-After": "100"})

    client = client_factory(handle)
    with pytest.raises(RouterClientError) as exc:
        if async_call:
            asyncio.run(client.async_classify("tasks", make_snapshot()))
        else:
            client.classify("tasks", make_snapshot())
    assert len(seen) == attempts
    assert exc.value.payload["attempts"] == attempts
    assert "test-key" not in str(exc.value)


def test_async_cancellation_does_not_start_fallback():
    primary = fake(error=asyncio.CancelledError())
    fallback = Mock()
    chain = RoutingChain(primary, fallback)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chain.async_classify("tasks", make_snapshot()))
    fallback.assert_not_called()


def test_concurrent_runs_share_transport_but_not_usage(client_factory):
    client = client_factory(lambda _: httpx.Response(200, json=body()))
    ledgers = [UsageSummary(), UsageSummary()]
    chains = [
        RoutingChain(client, Mock(), usage_accumulator=ledger) for ledger in ledgers
    ]

    async def run():
        return await asyncio.gather(
            *(
                chain.async_classify(str(i), make_snapshot())
                for i, chain in enumerate(chains)
            )
        )

    asyncio.run(run())
    assert [len(ledger.records) for ledger in ledgers] == [1, 1]
    assert ledgers[0].records is not ledgers[1].records


def test_defaults_and_invalid_settings(monkeypatch):
    monkeypatch.delenv("ROUTER_ENGINE", raising=False)
    monkeypatch.delenv("TYPESAFE_MODEL", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = load_settings()
    assert config.router_engine == "typesafe"
    assert config.typesafe_model == "jev-1.14.0"
    assert config.typesafe_api_key is None
    monkeypatch.setenv("ROUTER_ENGINE", "invalid")
    with pytest.raises(ValueError, match="ROUTER_ENGINE"):
        load_settings()


def test_runtime_rollback_and_missing_key_do_not_construct_jev(monkeypatch):
    import agents.agent_api.app.router.runtime as runtime
    from agents.agent_api.app.tracing import NULL_TRACE

    build = Mock(side_effect=AssertionError("should not construct Jev"))
    monkeypatch.setattr(runtime, "get_shared_typesafe_router", build)
    for config in (
        replace(runtime.settings, router_engine="llm", typesafe_api_key="key"),
        replace(runtime.settings, router_engine="typesafe", typesafe_api_key=None),
    ):
        monkeypatch.setattr(runtime, "settings", config)
        chain = runtime.build_runtime_router(Mock(), tracer=NULL_TRACE)
        assert chain.primary is None
    build.assert_not_called()


def test_owned_transports_close_and_reset(monkeypatch):
    import agents.agent_api.app.router.runtime as runtime

    client = TypeSafeRouterClient("key")

    async def run():
        # Create the native async pool without performing network IO.
        client._async_client = httpx.AsyncClient()
        pool = client._async_client
        monkeypatch.setattr(runtime, "_client", client)
        runtime.close_shared_typesafe_router()
        await runtime.close_shared_async_typesafe_router()
        assert pool.is_closed

    asyncio.run(run())
    assert client._client.is_closed
    assert runtime._client is None


@pytest.mark.parametrize(
    "value,expected", [(0.2499, False), (0.25, True), (0.5999, True), (0.60, False)]
)
def test_noul_boundaries(value, expected):
    decision = decision_from_answers(_answers(todoist=0.99, google_calendar=value))
    assert decision.uncertain is expected


def test_trace_boundary_excludes_private_request_data(client_factory):
    import json
    from langsmith import tracing_context
    from langsmith.run_helpers import get_current_run_tree

    trees = []

    def handle(request):
        trees.append(get_current_run_tree())
        return httpx.Response(200, json=body())

    client = client_factory(handle)
    snapshot = make_snapshot(custom_instructions="private user instructions")
    with tracing_context(enabled="local"):
        client.classify_detailed("private request content", snapshot)
    assert trees[0].inputs == {"provider": "typesafe"}
    assert "private" not in json.dumps(trees[0].outputs)
    assert trees[0].outputs["decision"]["domains"] == ["todoist"]


def test_application_lifespan_closes_jev_pools():
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    from agents.agent_api.app.main import create_app

    with (
        patch("agents.agent_api.app.db.verify_database_runtime"),
        patch("agents.agent_api.app.db.close_pool"),
        patch(
            "agents.agent_api.app.router.runtime.close_shared_typesafe_router"
        ) as close_sync,
        patch(
            "agents.agent_api.app.router.runtime.close_shared_async_typesafe_router",
            new_callable=AsyncMock,
        ) as close_async,
    ):
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200
    close_sync.assert_called_once_with()
    close_async.assert_awaited_once_with()
    from agents.agent_api.app import run_logging

    run_logging.reset_log_writer()


@pytest.mark.parametrize(
    "name,value",
    [
        ("TYPESAFE_ROUTER_REQUEST_TIMEOUT_SECONDS", "0"),
        ("TYPESAFE_ROUTER_REQUEST_TIMEOUT_SECONDS", "nan"),
        ("TYPESAFE_ROUTER_REQUEST_TIMEOUT_SECONDS", "inf"),
        ("TYPESAFE_ROUTER_MAX_RETRY_ATTEMPTS", "0"),
        ("TYPESAFE_ROUTER_MAX_RETRY_ATTEMPTS", "-1"),
        ("TYPESAFE_ROUTER_RETRY_MAX_DELAY_SECONDS", "0"),
        ("TYPESAFE_ROUTER_RETRY_MAX_DELAY_SECONDS", "nan"),
    ],
)
def test_typesafe_budget_settings_are_validated(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        load_settings()


def test_owned_native_transports_send_auth_and_reuse_pools(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body())

    transport = httpx.MockTransport(handle)
    sync_type, async_type = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: sync_type(transport=transport, **kwargs)
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: async_type(transport=transport, **kwargs)
    )
    client = TypeSafeRouterClient("secret-token", request_timeout_seconds=1.5)
    client.classify("first", make_snapshot())

    async def run():
        await client.async_classify("second", make_snapshot())
        pool = client._async_client
        await client.async_classify("third", make_snapshot())
        assert client._async_client is pool
        await client.aclose()
        assert pool.is_closed

    asyncio.run(run())
    client.close()
    assert len(requests) == 3
    assert all(
        request.headers["Authorization"] == "Bearer secret-token"
        for request in requests
    )
    assert all(
        request.headers["Content-Type"] == "application/json" for request in requests
    )
    assert all(request.extensions["timeout"]["read"] == 1.5 for request in requests)


def test_runtime_factory_shares_jev_but_keeps_run_context_separate(monkeypatch):
    import agents.agent_api.app.router.runtime as runtime
    from agents.agent_api.app.tracing import NULL_TRACE

    monkeypatch.setattr(
        runtime,
        "settings",
        replace(
            runtime.settings, router_engine="typesafe", typesafe_api_key="configured"
        ),
    )
    primary = fake(
        RouterDecision(
            outcome="conversation",
            domains=[],
            uncertain=False,
            candidate_domains=[],
            complexity="low",
        )
    )
    monkeypatch.setattr(
        runtime, "get_shared_typesafe_router", Mock(return_value=primary)
    )
    usages = [UsageSummary(), UsageSummary()]
    chains = [
        runtime.build_runtime_router(Mock(), tracer=NULL_TRACE, usage_accumulator=usage)
        for usage in usages
    ]
    assert chains[0].primary is chains[1].primary
    assert chains[0]._usage is usages[0]
    assert chains[1]._usage is usages[1]


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError],
)
@pytest.mark.parametrize("async_call", [False, True])
def test_transport_failures_retry_without_leaking_provider_messages(
    client_factory, failure, async_call
):
    requests = []

    def handle(request):
        requests.append(request)
        raise failure("private-provider-error")

    client = client_factory(handle)
    with pytest.raises(RouterClientError) as exc:
        if async_call:
            asyncio.run(client.async_classify("tasks", make_snapshot()))
        else:
            client.classify("tasks", make_snapshot())
    assert len(requests) == 2
    assert "private-provider-error" not in str(exc.value)


def test_trace_errors_do_not_include_raw_provider_exceptions(client_factory):
    from langsmith import tracing_context
    from langsmith.run_helpers import get_current_run_tree

    trees = []

    def handle(request):
        trees.append(get_current_run_tree())
        raise RuntimeError("private-provider-error")

    client = client_factory(handle)
    with tracing_context(enabled="local"):
        with pytest.raises(RouterClientError):
            client.classify("private query", make_snapshot())
    assert "private-provider-error" not in trees[0].error
