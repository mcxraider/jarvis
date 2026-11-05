"""Multimodal JEV wire contract, text retry, and image-aware selection caching."""

import asyncio
import base64
import io
import json
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image

from agents.agent_api.app.graph.nodes.orchestrator import UsageSummary
from agents.agent_api.app.router.client import RouterClientError
from agents.agent_api.app.router.images import image_cache_key, prepare_image_request
from agents.agent_api.app.router.runtime import RoutingChain
from agents.agent_api.app.router.typesafe import build_typesafe_request
from agents.agent_api.app.tools.selectors.router import RouterToolSelector
from tests.agents.runtime_helpers import make_snapshot
from tests.agents.test_router_selector import _ALL_TOOLS, _names, _registry
from tests.agents.test_typesafe_runtime import body, client_for, error, fake


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


def image(size=(400, 600), color="red", orientation=None):
    buffer = io.BytesIO()
    picture = Image.new("RGB", size, color)
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    picture.save(buffer, "JPEG", exif=exif, progressive=True)
    return {
        "image_url": "data:image/jpeg;base64,"
        + base64.b64encode(buffer.getvalue()).decode(),
        "detail": "high",
    }


def context(*images, prior=None):
    return {"images": images or (image(),), "prior_batches": prior}


def classify(client, async_call, **kwargs):
    method = client.async_classify if async_call else client.classify
    result = method("add this", make_snapshot(), **kwargs)
    return asyncio.run(result) if async_call else result


def multipart(request):
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: "
        + request.headers["content-type"].encode()
        + b"\r\n\r\n"
        + request.content
    )
    parts = list(message.iter_parts())
    assert parts[0].get_param("name", header="content-disposition") == "request"
    assert parts[0].get_content_type() == "application/json"
    return json.loads(parts[0].get_payload(decode=True)), parts[1:]


@pytest.mark.parametrize("async_call", [False, True])
def test_multipart_assets_and_per_request_headers(client_factory, async_call):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=body())

    client = client_factory(handle)
    images = context(image((2000, 1000), "red"), prior=((image((80, 120), "blue"),),))
    originals = deepcopy(images)
    assert classify(client, async_call, image_context=images).domains == ["todoist"]
    assert classify(client, async_call).domains == ["todoist"]
    request, parts = multipart(requests[0])
    assert requests[0].headers["TypeSafe-Danger-Zone-Images"] == "true"
    assert len(parts) == len(request["assets"]) == 2
    refs = [*request["state"]["prior_image_batches"][0], *request["state"]["images"]]
    assert set(refs) == set(request["assets"])
    for ref, part, size, color_channel in zip(
        refs, parts, [(280, 280), (1568, 784)], [2, 0]
    ):
        assert request["assets"][ref]["part"] == part.get_param(
            "name", header="content-disposition"
        )
        assert part.get_content_type() == "image/jpeg"
        with Image.open(io.BytesIO(part.get_payload(decode=True))) as prepared:
            assert prepared.size == size
            assert not prepared.info.get("progressive")
            assert prepared.getpixel((size[0] // 2, size[1] // 2))[color_channel] > 200
    assert "assets" not in json.loads(requests[1].content)
    assert "TypeSafe-Danger-Zone-Images" not in requests[1].headers
    assert requests[1].headers["content-type"] == "application/json"
    assert images == originals


def test_preparation_applies_exif_and_detailed_metadata_has_no_bytes():
    request = build_typesafe_request("add this", make_snapshot())
    images = context(image((400, 600), orientation=6))
    kwargs = prepare_image_request(request, images)
    with Image.open(io.BytesIO(kwargs["files"][1][1][1])) as prepared:
        assert prepared.size == (600, 400)
        assert not prepared.getexif()
    assert "data_base64" not in json.dumps(request)
    assert images["images"][0]["image_url"] not in json.dumps(request)


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize(
    "failure",
    ["400", "413", "429", "503", "timeout", "invalid", "uncertain", "preparation"],
)
def test_one_image_attempt_then_immediate_text_retry(
    client_factory, async_call, failure
):
    requests = []
    usage = UsageSummary()
    tracer = Mock()

    def handle(request):
        requests.append(request)
        if "TypeSafe-Danger-Zone-Images" in request.headers:
            if failure.isdigit():
                return httpx.Response(int(failure), json={"message": "rejected"})
            if failure == "timeout":
                raise httpx.ReadTimeout("failed", request=request)
            value = body()
            if failure == "invalid":
                value["answers"] = {}
            elif failure == "uncertain":
                value["answers"]["needs_todoist"]["noul"] = 0.4
            return httpx.Response(200, json=value)
        return httpx.Response(200, json=body())

    client = client_factory(handle)
    fallback = Mock(side_effect=AssertionError("Text JEV succeeded"))
    chain = RoutingChain(client, fallback, tracer=tracer, usage_accumulator=usage)
    images = (
        context(
            {"image_url": "data:image/jpeg;base64,SECRET-INVALID", "detail": "high"}
        )
        if failure == "preparation"
        else context()
    )
    assert classify(chain, async_call, image_context=images).domains == ["todoist"]
    assert len(requests) == (1 if failure == "preparation" else 2)
    assert "TypeSafe-Danger-Zone-Images" not in requests[-1].headers
    assert "assets" not in json.loads(requests[-1].content)
    assert len(usage.records) == (2 if failure in {"invalid", "uncertain"} else 1)
    assert any(
        call.args[0] == "router.image_fallback" for call in tracer.event.call_args_list
    )
    assert "SECRET-INVALID" not in str(tracer.mock_calls)
    assert images["images"][0]["image_url"] not in str(tracer.mock_calls)


@pytest.mark.parametrize("async_call", [False, True])
def test_text_retry_retains_retries_and_fallback_has_no_images(
    client_factory, async_call
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(503)

    client = client_factory(handle)
    fallback = fake(body_decision())
    chain = RoutingChain(client, lambda: fallback)
    result = classify(chain, async_call, image_context=context())
    assert result.domains == ["todoist"]
    assert len(requests) == 3  # one image attempt, two text attempts
    method = fallback.async_classify if async_call else fallback.classify
    assert "image_context" not in method.call_args.kwargs


def body_decision():
    from agents.agent_api.app.router.typesafe import decision_from_answers

    return decision_from_answers(body()["answers"])


@pytest.mark.parametrize("async_call", [False, True])
def test_absent_primary_goes_directly_to_text_fallback(async_call):
    fallback = fake(body_decision())
    chain = RoutingChain(None, lambda: fallback)
    assert classify(chain, async_call, image_context=context()).domains == ["todoist"]
    method = fallback.async_classify if async_call else fallback.classify
    assert "image_context" not in method.call_args.kwargs


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_decision_and_failure_cache_includes_image_content_and_order(async_call, fails):
    primary = fake(body_decision(), error=error() if fails else None)
    fallback = fake(error=error())
    selector = RouterToolSelector(
        RoutingChain(primary, lambda: fallback), make_snapshot()
    )
    first, second = image(color="red"), image(color="blue")
    inputs = [
        context(first, second),
        context(first, second),
        context(second, first),
        context(first),
        None,
    ]

    async def run():
        return [
            await selector.async_select_schemas(
                "same caption", _registry(), image_context=images
            )
            for images in inputs
        ]

    results = (
        asyncio.run(run())
        if async_call
        else [
            selector.select_schemas("same caption", _registry(), image_context=images)
            for images in inputs
        ]
    )
    method = primary.async_classify if async_call else primary.classify
    assert method.call_count == (7 if fails else 4)
    assert results[0] == results[1]
    if fails:
        assert all(_names(result) == _ALL_TOOLS for result in results)
    assert image_cache_key("caption", context(first, second)) != image_cache_key(
        "caption", context(second, prior=((first,),))
    )


def test_async_image_cancellation_does_not_retry(client_factory):
    requests = []

    async def handle(request):
        requests.append(request)
        raise asyncio.CancelledError()

    client = client_factory(handle)
    fallback = Mock()
    chain = RoutingChain(client, fallback)
    with pytest.raises(asyncio.CancelledError):
        classify(chain, True, image_context=context())
    assert len(requests) == 1
    fallback.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_real_router_in_orchestrator_preserves_pins_and_model_images(
    client_factory, fails
):
    from agents.agent_api.app.graph.nodes.orchestrator import create_agent_node
    from agents.agent_api.app.graph.run_deps import CONFIGURABLE_DEPS_KEY, RunDeps
    from tests.agents.test_agent_node_router import (
        RecordingClient,
        _state_turn0,
    )
    from tests.agents.test_agent_node_router import (
        _registry as node_registry,
    )

    requests = []

    def handle(request):
        requests.append(request)
        if fails and "TypeSafe-Danger-Zone-Images" in request.headers:
            return httpx.Response(400)
        return httpx.Response(200, json=body())

    snapshot = make_snapshot(active=("todoist", "google_calendar"))
    selector = RouterToolSelector(
        RoutingChain(client_factory(handle), Mock()), snapshot
    )
    state = _state_turn0(snapshot, user_prompt="add these to todoist")
    state["active_domains"] = ["google_calendar"]
    state["clarification_history"] = [{"question": "Which list?", "reply": "Inbox"}]
    historical, prior, current = image(color="green"), image(color="blue"), image()
    deps = RunDeps(
        images=(current,),
        prior_image_batches=((prior,),),
        previous_thread_images=(historical,),
        previous_thread_context="Previous conversation reference",
    )
    model = RecordingClient()
    node = create_agent_node(
        model, node_registry(), max_agent_turns=30, tool_selector=selector
    )
    result = asyncio.run(node(state, {"configurable": {CONFIGURABLE_DEPS_KEY: deps}}))
    request, parts = multipart(requests[0])
    assert len(parts) == 2  # previous-thread image remains model-only
    assert "Inbox" in request["state"]["user_request"]
    assert model.seen_kwargs["image_context"] == {
        "images": (current,),
        "prior_batches": ((historical,), (prior,)),
    }
    assert {"add_todoist_task", "list_calendar_events"} <= set(
        result["selected_tool_names"]
    )
    assert "## Todoist tool tips" in model.seen_messages[0]["content"]
    assert "## Google Calendar tool tips" in model.seen_messages[0]["content"]
    assert len(requests) == (2 if fails else 1)


@pytest.mark.parametrize("async_call", [False, True])
def test_uncertain_text_retry_keeps_existing_candidate_fallback(async_call):
    uncertain = body_decision().model_copy(
        update={"uncertain": True, "candidate_domains": ["todoist"]}
    )
    primary = fake()
    method = primary.async_classify if async_call else primary.classify
    method.side_effect = [error(), uncertain]
    chain = RoutingChain(primary, lambda: fake(error=error()))
    assert classify(chain, async_call, image_context=context()) is uncertain
    assert method.call_count == 2


@pytest.mark.parametrize("async_call", [False, True])
def test_preparation_error_payload_is_redacted(client_factory, async_call):
    client = client_factory(
        Mock(side_effect=AssertionError("No upload for invalid images"))
    )
    with pytest.raises(RouterClientError) as caught:
        classify(
            client,
            async_call,
            image_context=context(
                {"image_url": "data:image/jpeg;base64,SECRET-INVALID"}
            ),
        )
    assert caught.value.payload["type"] == "image_preparation"
    assert "SECRET-INVALID" not in str(caught.value)
    assert "SECRET-INVALID" not in str(caught.value.payload)


@pytest.mark.parametrize("async_call", [False, True])
def test_langsmith_submissions_exclude_image_bytes(client_factory, async_call):
    from unittest.mock import MagicMock

    from langsmith import tracing_context

    tracing_client = MagicMock()
    client = client_factory(lambda _: httpx.Response(200, json=body()))
    images = context()
    with tracing_context(enabled=True, client=tracing_client):
        classify(client, async_call, image_context=images)
    assert tracing_client.create_run.called
    assert tracing_client.update_run.called
    submitted = str(tracing_client.mock_calls)
    assert images["images"][0]["image_url"] not in submitted
    assert "data:image/jpeg;base64" not in submitted
    assert "data_base64" not in submitted
