"""LangGraph builder: graph factory, initial state, and run entrypoints."""

import asyncio
import logging
import threading
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from langgraph.types import Command

from agents.agent_api.app.checkpointing import (
    DEFAULT_CHECKPOINTER,
    as_async_checkpointer,
    ensure_default_checkpointer_setup,
    get_async_checkpointer,
)
from agents.agent_api.app.async_offload import bounded_to_thread
from agents.agent_api.app.config import settings
from agents.agent_api.app.constants import (
    ALLOW_MUTATIONS,
    DEEPSEEK_MODEL,
    LANGSMITH_TAGS,
    MAX_AGENT_TURNS,
    USER_ID,
)
from agents.agent_api.app.graph.assembly import NodeSpec, build_graph
from agents.agent_api.app.graph.edges import (
    route_after_agent,
    route_after_confirm,
    route_after_tools,
    route_by_next,
)
from agents.agent_api.app.graph.nodes.confirm import create_confirm_node
from agents.agent_api.app.graph.nodes.executor import create_executor_node
from agents.agent_api.app.graph.nodes.hitl import create_hitl_node
from agents.agent_api.app.graph.nodes.orchestrator import (
    UsageSummary,
    create_agent_node,
    get_shared_agent_client,
)
from agents.agent_api.app.router.model_router import ModelRouter, create_default_model_router
from agents.agent_api.app.graph.nodes.prepare_confirm import create_prepare_confirm_node
from agents.agent_api.app.graph.nodes.summarize import create_summarize_node
from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.graph.nodes.validate_entities import create_validate_entities_node
from agents.agent_api.app.graph.prompts import (
    USER_PROMPT,
    build_initial_messages,
)
from agents.agent_api.app.graph.run_deps import RunDeps
from agents.agent_api.app.graph.run_control import RunControl
from agents.agent_api.app.graph.state import JarvisState, enrich_interrupt_status
from agents.agent_api.app.idempotency import DEFAULT_IDEMPOTENCY_STORE, IdempotencyStore
from agents.agent_api.app.pricing import (
    calculate_cost_usd,
    derive_uncached_input_tokens,
)
from agents.agent_api.app.run_logging import (
    FileLoggingTracer,
    RunLogIdentity,
    format_singapore_log_iso,
    open_run_log,
)
from agents.agent_api.app.tools.base import ToolRegistry
from agents.agent_api.app.tools.dispatcher import ToolDispatcher
from agents.agent_api.app.tools.registry_factory import (
    apply_registered_tools,
    build_registry_from_clients,
    build_runtime_registry,
)
from agents.agent_api.app.tools.selection import ToolSelector, get_selector
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter
from agents.agent_api.app.user_context.resolver import (
    load_thread_runtime_context_async,
    resolve_runtime_context_async,
    store_thread_context_async,
)
from agents.agent_api.app.user_context.identity import TelegramIdentity, telegram_identity
from agents.agent_api.app.user_context.runtime import (
    ResolvedRuntimeContext,
    RuntimeContextSnapshot,
)


_builder_logger = logging.getLogger(__name__)
_USE_DEFAULT_CHECKPOINTER = object()
_SYNC_RUNNER: Optional[asyncio.Runner] = None
_SYNC_RUNNER_LOCK = threading.Lock()


def _retarget_tracer(client, tracer: TracePrinter):
    """Rebind a client's tracer for this run, preferring immutable with_tracer."""
    if hasattr(client, "with_tracer"):
        return client.with_tracer(tracer)
    if hasattr(client, "tracer"):
        client.tracer = tracer
    return client


def _register_thread(
    thread_id: str,
    identity: Optional[TelegramIdentity],
    user_prompt: str,
    status: str,
    resuming: bool,
) -> None:
    """Upsert thread metadata. Fire-and-forget — never crashes the request."""
    if identity is None:
        return
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if resuming:
                    cur.execute(
                        """
                        UPDATE threads
                        SET message_count = message_count + 1,
                            status = %s,
                            updated_at = NOW()
                        WHERE thread_id = %s
                        """,
                        (status, thread_id),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO threads (
                            thread_id, user_id, title, status, message_count
                        )
                        VALUES (
                            %s,
                            public.resolve_user_id(%s),
                            LEFT(%s, 100),
                            %s,
                            1
                        )
                        ON CONFLICT (thread_id) DO UPDATE
                        SET message_count = threads.message_count + 1,
                            status = EXCLUDED.status,
                            updated_at = NOW()
                        """,
                        (
                            thread_id,
                            identity.telegram_id,
                            user_prompt,
                            status,
                        ),
                    )
    except Exception as exc:
        _builder_logger.warning(
            "Thread registration failed (non-fatal).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )


def _log_usage(
    identity: Optional[TelegramIdentity],
    thread_id: str,
    usage: "UsageSummary",
    latency_ms: int,
    model: str,
) -> None:
    """Write usage telemetry to Supabase. Fire-and-forget."""
    if identity is None:
        return
    if not usage.total_tokens:
        _builder_logger.warning(
            "Usage logging skipped because token usage is absent.",
            extra={"thread_id": thread_id, "model": model},
        )
        return
    prompt_tokens = usage.prompt_tokens or 0
    cached_tokens: Optional[int] = usage.cached_tokens or 0
    output_tokens = usage.completion_tokens or 0
    uncached_tokens: Optional[int] = None
    cost_usd: Optional[Decimal] = None
    try:
        uncached_tokens = derive_uncached_input_tokens(prompt_tokens, cached_tokens)
        cost_usd = calculate_cost_usd(
            model,
            prompt_tokens,
            cached_tokens,
            output_tokens,
        )
        if cost_usd is None:
            _builder_logger.warning(
                "Usage cost unavailable for unpriced model.",
                extra={"thread_id": thread_id, "model": model},
            )
    except ValueError as exc:
        cached_tokens = None
        _builder_logger.warning(
            "Usage cost unavailable for invalid token metadata.",
            extra={"thread_id": thread_id, "model": model, "error": str(exc)},
        )
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO usage_logs (user_id, thread_id, event_type, model,
                                           input_tokens, cached_input_tokens,
                                           uncached_input_tokens, output_tokens,
                                           cost_usd, latency_ms)
                    VALUES (
                        public.resolve_user_id(%s),
                        %s,
                        'run',
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s
                    )
                    """,
                    (
                        identity.telegram_id,
                        thread_id,
                        model,
                        prompt_tokens,
                        cached_tokens,
                        uncached_tokens,
                        output_tokens,
                        cost_usd,
                        latency_ms,
                    ),
                )
    except Exception as exc:
        _builder_logger.warning(
            "Usage logging failed (non-fatal).",
            extra={
                "thread_id": thread_id,
                "error": type(exc).__name__,
                "error_message": str(exc),
            },
        )


def create_jarvis_graph(
    agent_client: Any = None,
    tool_dispatcher: Optional[ToolDispatcher] = None,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    checkpointer: Any = _USE_DEFAULT_CHECKPOINTER,
    tool_selector: Optional[ToolSelector] = None,
    model_router: Optional[ModelRouter] = None,
    usage_accumulator: Optional[UsageSummary] = None,
):
    """Create the Jarvis LangGraph app from declarative node specs.

    The tool catalogue the agent sees comes from ``tool_dispatcher.registry``, so
    new tool domains plug in via the registry and new nodes/stages via NodeSpec —
    neither requires editing this function's body beyond the spec list.
    ``tool_selector`` decides which of those tools the model sees each turn
    (default: the whole catalogue; see ``tools/selection.py``).
    """

    tracer = tracer or NULL_TRACE
    if checkpointer is _USE_DEFAULT_CHECKPOINTER:
        checkpointer = DEFAULT_CHECKPOINTER
    # A production graph is compiled without request objects. Nodes resolve those
    # objects from RunDeps in the invocation config; these values remain as direct-
    # call/Studio fallbacks for compatibility with existing tests and tooling.
    registry = tool_dispatcher.registry if tool_dispatcher is not None else None

    node_specs = [
        NodeSpec(
            name="agent",
            node=create_agent_node(
                agent_client,
                registry,
                max_agent_turns,
                tracer,
                tool_selector=tool_selector,
                model_router=model_router,
                usage_accumulator=usage_accumulator,
            ),
            router=route_after_agent,
            route_map={
                "hitl": "hitl",
                "validate": "validate_entities",
                "end": "end",
            },
        ),
        NodeSpec(
            name="validate_entities",
            node=create_validate_entities_node(tracer),
            router=route_by_next,
            route_map={
                "tools": "tools",
                "prepare_confirm": "prepare_confirm",
                "agent": "agent",
            },
        ),
        NodeSpec(
            name="tools",
            node=create_tools_node(tool_dispatcher, tracer),
            router=route_after_tools,
            route_map={"agent": "agent", "summarize": "summarize"},
        ),
        NodeSpec(name="summarize", node=create_summarize_node(tracer), static_route="agent"),
        NodeSpec(name="hitl", node=create_hitl_node(tracer), static_route="agent"),
        NodeSpec(
            name="prepare_confirm",
            node=create_prepare_confirm_node(tracer),
            static_route="confirm",
        ),
        NodeSpec(
            name="confirm",
            node=create_confirm_node(tracer),
            router=route_after_confirm,
            route_map={"approve": "executor", "decline": "end"},
        ),
        NodeSpec(
            name="executor",
            node=create_executor_node(tool_dispatcher, tracer),
            static_route="agent",
        ),
    ]

    return build_graph(JarvisState, node_specs, entry="agent", checkpointer=checkpointer)


_compiled_graphs: dict[int, tuple[Any, Any]] = {}
_compiled_graphs_lock = threading.Lock()


def get_or_compile_graph(checkpointer: Any = _USE_DEFAULT_CHECKPOINTER) -> Any:
    """Return the shared compiled topology for a checkpointer.

    Request-scoped clients, routing, tracing, and mutation policy are supplied in
    ``RunDeps`` on each invocation, so reusing the compiled topology cannot bind one
    user's runtime objects into another run. Retaining the checkpointer alongside
    the graph prevents its object id from being recycled while the entry is cached.
    """

    if checkpointer is _USE_DEFAULT_CHECKPOINTER:
        checkpointer = DEFAULT_CHECKPOINTER
    key = id(checkpointer)
    cached = _compiled_graphs.get(key)
    if cached is not None:
        return cached[1]
    with _compiled_graphs_lock:
        cached = _compiled_graphs.get(key)
        if cached is None:
            cached = (checkpointer, create_jarvis_graph(checkpointer=checkpointer))
            _compiled_graphs[key] = cached
        return cached[1]


def reset_compiled_graphs() -> None:
    """Clear the compiled-graph cache for deterministic test isolation."""

    with _compiled_graphs_lock:
        _compiled_graphs.clear()


def build_initial_state(
    user_prompt: str,
    user_id: str = USER_ID,
    thread_id: Optional[str] = None,
    request_source: str = "api",
    timezone: Optional[str] = None,
    user_name: Optional[str] = None,
    runtime_context: Optional[RuntimeContextSnapshot] = None,
    registered_tools: Optional[list] = None,
) -> JarvisState:
    """Create a fresh state object for one Jarvis run."""

    thread_id = thread_id or str(uuid.uuid4())
    return {
        "messages": build_initial_messages(
            user_prompt,
            timezone=timezone,
            user_name=user_name,
            runtime_context=runtime_context,
            registered_tools=registered_tools,
        ),
        "user_prompt": user_prompt,
        "user_id": user_id,
        "request_source": request_source,
        "thread_id": thread_id,
        "turn_count": 0,
        "tool_results": [],
        "pending_clarification": {},
        "clarification_history": [],
        "interrupted": False,
        "interrupt_payload": {},
        "final_response": "",
        "error": "",
        "next": "agent",
        "held_calls": None,
        "pending_interrupt": None,
        "confirm_decision": None,
        "consumed_call_ids": [],
        "runtime_context": (
            runtime_context.model_dump(mode="json")
            if runtime_context is not None
            else {}
        ),
    }


def _build_runtime_metadata(
    runtime_context: Optional[ResolvedRuntimeContext],
    registry: ToolRegistry,
) -> dict:
    """Return the secret-free runtime fields attached to trace metadata."""

    if runtime_context is None:
        return {
            "runtime_context_schema": None,
            "preference_revision": None,
            "active_domains": [],
            "registered_tools": [spec.name for spec in registry.specs],
        }
    snapshot = runtime_context.snapshot
    return {
        "runtime_context_schema": snapshot.schema_version,
        "preference_revision": snapshot.preference_revision,
        "active_domains": sorted(snapshot.active_providers()),
        "registered_tools": list(snapshot.registered_tools),
    }


def _resolve_tool_selector(
    runtime_context: Optional[ResolvedRuntimeContext],
    resuming: bool,
    allow_mutations: bool,
    tracer: TracePrinter,
    *,
    router_enabled: bool,
    tool_selector_name: str,
) -> ToolSelector:
    """Choose the tool selector for this run, honoring the opt-in router gate.

    The query router is built ONLY when every condition holds:
    ``router_enabled`` and ``tool_selector_name == "router"`` and a runtime
    snapshot exists. Resumed runs use the router too so clarification turns keep
    the same narrow tool surface instead of falling back to every connected tool.
    It is also never a hard-failure path:
    constructing the ``RouterClient`` (e.g. a missing API key) degrades to the
    static, all-tools selector, which is also the router's per-turn fallback.

    Outside the gate the configured selector is honored — ``"static"``/``"keyword"``
    as named; a ``"router"`` request whose gate is unmet degrades to ``"static"``.
    """

    use_router = (
        router_enabled
        and tool_selector_name == "router"
        and runtime_context is not None
    )
    if use_router:
        try:
            from agents.agent_api.app.router.client import get_shared_router_client

            router_client = get_shared_router_client().with_tracer(tracer)
        except Exception as error:  # noqa: BLE001 — router must never fail the run
            tracer.event(
                "router.disabled",
                "Router client unavailable; using static selector.",
                error=str(error),
            )
            return get_selector("static", allow_mutations=allow_mutations)
        return get_selector(
            "router",
            router_client=router_client,
            snapshot=runtime_context.snapshot,
            tracer=tracer,
            fallback_selector=get_selector("static", allow_mutations=allow_mutations),
        )

    name = tool_selector_name if tool_selector_name in {"static", "keyword"} else "static"
    return get_selector(name, allow_mutations=allow_mutations)


async def run_jarvis_async(
    user_prompt: str = USER_PROMPT,
    user_id: str = USER_ID,
    request_source: str = "api",
    allow_mutations: bool = ALLOW_MUTATIONS,
    agent_client: Optional[Any] = None,
    todoist_client: Optional[Any] = None,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    thread_id: Optional[str] = None,
    identity: Optional[TelegramIdentity] = None,
    # Deprecated compatibility inputs; API callers should send ``identity``.
    telegram_user_id: Optional[int] = None,
    telegram_username: Optional[str] = None,
    telegram_first_name: Optional[str] = None,
    clarification_reply: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    request_id: Optional[str] = None,
    tool_selector: Optional[ToolSelector] = None,
    idempotency_store: Optional[IdempotencyStore] = None,
    run_control: Optional[RunControl] = None,
) -> JarvisState:
    """Run the full Jarvis graph natively on the caller's event loop.

    Each call is one LangSmith trace named ``jarvis.invoke`` or ``jarvis.resume``,
    correlated by ``request_id`` and grouped by ``thread_id``. ``request_id`` is
    generated when callers omit it so every run is traceable.
    """

    if clarification_reply is not None and not thread_id:
        raise ValueError("thread_id is required when resuming with clarification_reply.")
    if identity is None and telegram_user_id is not None:
        identity = telegram_identity(
            telegram_user_id,
            telegram_username,
            telegram_first_name,
        )
    thread_id = thread_id or str(uuid.uuid4())
    request_id = request_id or str(uuid.uuid4())
    if checkpointer is None:
        checkpointer = get_async_checkpointer()
    # A caller-supplied selector (DI/tests) is honored as-is; otherwise the default
    # is resolved later — after the runtime context and tracer are known — so the
    # opt-in router selector can be built against the resolved snapshot (see below).
    if idempotency_store is None:
        idempotency_store = DEFAULT_IDEMPOTENCY_STORE

    resuming = clarification_reply is not None
    invocation_type = "resume" if resuming else "invoke"
    run_name = f"jarvis.{invocation_type}"
    started_at = datetime.now()

    runtime_context = None
    if identity is not None and settings.postgres_dsn:
        runtime_context = (
            await load_thread_runtime_context_async(thread_id, identity)
            if resuming
            else await resolve_runtime_context_async(identity)
        )

    base_tracer = tracer if tracer is not None else TracePrinter()
    run_log_identity = RunLogIdentity(
        request_source=request_source,
        identity=identity,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        telegram_first_name=telegram_first_name,
    )
    run_log = await bounded_to_thread(open_run_log, thread_id, run_log_identity)
    if run_log is not None:
        run_log.write_header(
            started_at=format_singapore_log_iso(started_at),
            request_id=request_id,
            thread_id=thread_id,
            user_id=user_id,
            telegram_id=identity.telegram_id if identity else None,
            identity_username=identity.username if identity else None,
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            telegram_first_name=telegram_first_name,
            request_source=request_source,
            invocation_type=invocation_type,
            model=DEEPSEEK_MODEL,
            allow_mutations=allow_mutations,
            max_agent_turns=max_agent_turns,
            resuming=resuming,
        )
        tracer = FileLoggingTracer(base_tracer, run_log)
    else:
        tracer = base_tracer

    tracer.section("Jarvis LangGraph Run")
    tracer.event(
        "runtime.start",
        "Starting graph invocation.",
        request_id=request_id,
        model=DEEPSEEK_MODEL,
        allow_mutations=allow_mutations,
        max_turns=max_agent_turns,
        thread_id=thread_id,
        request_source=request_source,
        resuming=resuming,
    )
    tracer.progress({"phase": "request", "action": "started"})
    tracer.payload("runtime.prompt", "user_prompt", user_prompt)

    if agent_client is None:
        agent_client = get_shared_agent_client(tracer=tracer)
    else:
        agent_client = _retarget_tracer(agent_client, tracer)
    run_usage = UsageSummary()
    offline_tool_names: Optional[list] = None
    if runtime_context is not None:
        registry, run_clients, tool_names_by_provider = build_runtime_registry(
            runtime_context, tracer
        )
        apply_registered_tools(runtime_context, registry, tool_names_by_provider)
        if not resuming:
            # Resume ownership and provider credentials depend on this snapshot.
            # Persist it before the graph can reach an interrupt checkpoint.
            await store_thread_context_async(
                thread_id,
                user_prompt,
                runtime_context.snapshot,
            )
    else:
        # Offline / dependency-injection path: run with caller-supplied clients.
        # No credential resolution happens here — production always resolves a
        # runtime context above.
        todoist_client = todoist_client or TodoistApiClient(tracer=tracer)
        todoist_client = _retarget_tracer(todoist_client, tracer)
        registry = build_registry_from_clients(todoist_client=todoist_client)
        run_clients = [todoist_client]
        offline_tool_names = [spec.name for spec in registry.specs]
    dispatcher = ToolDispatcher(
        registry,
        allow_mutations=allow_mutations,
        tracer=tracer,
        run_control=run_control,
        idempotency_store=idempotency_store,
        idempotency_operation_ttl_seconds=settings.idempotency_operation_ttl_seconds,
        idempotency_lease_seconds=settings.idempotency_lease_seconds,
        idempotency_wait_seconds=settings.idempotency_wait_seconds,
        idempotency_poll_interval_seconds=settings.idempotency_poll_interval_seconds,
    )
    if tool_selector is None:
        tool_selector = _resolve_tool_selector(
            runtime_context,
            resuming,
            allow_mutations,
            tracer,
            router_enabled=settings.router_enabled,
            tool_selector_name=settings.tool_selector,
        )
    model_router = create_default_model_router(
        enabled=settings.model_router_enabled,
        default_model=settings.model_router_default_model,
        default_reasoning=settings.model_router_default_reasoning,
        complex_model=settings.model_router_complex_model,
        complex_reasoning=settings.model_router_complex_reasoning,
        multi_domain_reasoning=settings.model_router_multi_domain_reasoning,
    )
    run_deps = RunDeps(
        agent_client=agent_client,
        registry=registry,
        dispatcher=dispatcher,
        tracer=tracer,
        tool_selector=tool_selector,
        model_router=model_router,
        usage_accumulator=run_usage,
        max_agent_turns=max_agent_turns,
        run_control=run_control,
    )
    app = get_or_compile_graph(checkpointer)
    config = {
        "configurable": {"thread_id": thread_id, "deps": run_deps},
        "run_name": run_name,
        "tags": [*LANGSMITH_TAGS, invocation_type],
        "metadata": {
            "request_id": request_id,
            "thread_id": thread_id,
            "invocation_type": invocation_type,
            "user_id": user_id,
            "telegram_id": identity.telegram_id if identity else None,
            "telegram_username": identity.username if identity else None,
            "request_source": request_source,
            "model": DEEPSEEK_MODEL,
            "allow_mutations": allow_mutations,
            "max_agent_turns": max_agent_turns,
            **_build_runtime_metadata(runtime_context, registry),
        },
    }
    tracer.event(
        "runtime.graph", "Compiled graph.", nodes="agent, validate_entities, hitl, tools"
    )
    # Native LangSmith tracing (LangGraph node spans + @traceable / wrap_openai
    # child spans) is governed by the LANGSMITH_TRACING env var. Tracing is
    # best-effort: callback failures never propagate into the graph result.
    try:
        if resuming:
            result = await app.ainvoke(Command(resume=clarification_reply), config)
        else:
            result = await app.ainvoke(
                build_initial_state(
                    user_prompt,
                    user_id=user_id,
                    thread_id=thread_id,
                    request_source=request_source,
                    timezone=(
                        runtime_context.snapshot.timezone
                        if runtime_context is not None
                        else None
                    ),
                    user_name=(
                        runtime_context.snapshot.display_name
                        if runtime_context is not None
                        else None
                    ),
                    runtime_context=(
                        runtime_context.snapshot
                        if runtime_context is not None
                        else None
                    ),
                    registered_tools=offline_tool_names,
                ),
                config,
            )
    except asyncio.CancelledError:
        # Intentional run cancellation/deadline is a controlled terminal path,
        # not a graph crash. The API producer persists its terminal response.
        raise
    except BaseException as exc:
        if run_log is not None:
            await bounded_to_thread(run_log.write_crash, exc)
        raise
    result = enrich_interrupt_status(result, thread_id)

    thread_status = "interrupted" if result.get("interrupted") else "completed"
    await bounded_to_thread(
        _register_thread,
        thread_id,
        identity,
        user_prompt,
        thread_status,
        resuming,
    )

    # Production DeepSeek clients write into the explicitly run-scoped
    # accumulator. Preserve compatibility with injected/duck-typed clients that
    # expose only their legacy ``usage`` attribute.
    client_usage = getattr(agent_client, "usage", None)
    usage = (
        run_usage
        if any(run_usage.as_dict().values()) or client_usage is None
        else client_usage
    )
    finished_at = datetime.now()
    tracer.event(
        "runtime.done",
        "Graph invocation completed.",
        request_id=request_id,
        turns=result.get("turn_count"),
        tool_results=len(result.get("tool_results", [])),
        has_error=bool(result.get("error")),
        interrupted=bool(result.get("interrupted")),
        interrupt_type=result.get("pending_interrupt"),
        total_tokens=usage.total_tokens or None,
    )
    if run_log is not None:
        cache_hit_rate = (
            round(usage.cached_tokens / usage.prompt_tokens * 100, 1)
            if usage.prompt_tokens > 0 and usage.cached_tokens > 0
            else 0.0
        )
        run_log.write_footer(
            finished_at=format_singapore_log_iso(finished_at),
            duration_seconds=round((finished_at - started_at).total_seconds(), 3),
            request_id=request_id,
            turns=result.get("turn_count"),
            tool_results=len(result.get("tool_results", [])),
            has_error=bool(result.get("error")),
            interrupted=bool(result.get("interrupted")),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_tokens=usage.cached_tokens,
            cache_hit_rate=cache_hit_rate,
            reasoning_tokens=usage.reasoning_tokens,
        )
    if result.get("error"):
        tracer.progress({"phase": "failed", "action": "failed"})
    elif result.get("interrupted"):
        tracer.progress({
            "phase": "awaiting_confirmation",
            "action": "waiting",
            "intent": "confirm" if result.get("pending_interrupt") == "confirm" else "clarify",
        })
    else:
        tracer.progress({"phase": "finalizing", "action": "completed"})

    if run_log is not None:
        result["run_log_path"] = str(run_log.path.resolve())

    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    await bounded_to_thread(
        _log_usage,
        identity,
        thread_id,
        usage,
        duration_ms,
        DEEPSEEK_MODEL,
    )

    return result


def run_jarvis(
    user_prompt: str = USER_PROMPT,
    user_id: str = USER_ID,
    request_source: str = "api",
    allow_mutations: bool = ALLOW_MUTATIONS,
    agent_client: Optional[Any] = None,
    todoist_client: Optional[Any] = None,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    thread_id: Optional[str] = None,
    identity: Optional[TelegramIdentity] = None,
    telegram_user_id: Optional[int] = None,
    telegram_username: Optional[str] = None,
    telegram_first_name: Optional[str] = None,
    clarification_reply: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    request_id: Optional[str] = None,
    tool_selector: Optional[ToolSelector] = None,
    idempotency_store: Optional[IdempotencyStore] = None,
    run_control: Optional[RunControl] = None,
) -> JarvisState:
    """Synchronous CLI/test adapter around :func:`run_jarvis_async`.

    The adapter reuses one CLI-owned event loop so pooled async transports remain
    loop-safe across sequential prompts and HITL resumes. Async API routes must
    call ``run_jarvis_async`` directly.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "run_jarvis cannot be called from an active event loop; "
            "await run_jarvis_async instead."
        )
    if checkpointer is None:
        checkpointer = DEFAULT_CHECKPOINTER
        ensure_default_checkpointer_setup()
    checkpointer = as_async_checkpointer(checkpointer)
    global _SYNC_RUNNER
    invocation = run_jarvis_async(
        user_prompt=user_prompt,
        user_id=user_id,
        request_source=request_source,
        allow_mutations=allow_mutations,
        agent_client=agent_client,
        todoist_client=todoist_client,
        max_agent_turns=max_agent_turns,
        tracer=tracer,
        thread_id=thread_id,
        identity=identity,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        telegram_first_name=telegram_first_name,
        clarification_reply=clarification_reply,
        checkpointer=checkpointer,
        request_id=request_id,
        tool_selector=tool_selector,
        idempotency_store=idempotency_store,
        run_control=run_control,
    )
    with _SYNC_RUNNER_LOCK:
        if _SYNC_RUNNER is None:
            _SYNC_RUNNER = asyncio.Runner()
        try:
            return _SYNC_RUNNER.run(invocation)
        except BaseException:
            invocation.close()
            raise


def shutdown_sync_runner() -> None:
    """Close CLI-owned async transports on their owning loop."""

    global _SYNC_RUNNER
    with _SYNC_RUNNER_LOCK:
        runner = _SYNC_RUNNER
        if runner is None:
            return

        async def close_resources() -> None:
            from agents.agent_api.app.graph.nodes.orchestrator import (
                close_shared_async_agent_client,
            )
            from agents.agent_api.app.graph.nodes.summarize import (
                close_shared_async_summarizer_client,
            )
            from agents.agent_api.app.router.client import (
                close_shared_async_router_openai_client,
            )
            from agents.agent_api.app.tools.todoist.client import (
                close_todoist_async_http_client,
            )

            await close_shared_async_agent_client()
            await close_shared_async_router_openai_client()
            await close_shared_async_summarizer_client()
            await close_todoist_async_http_client()

        try:
            runner.run(close_resources())
        finally:
            runner.close()
            _SYNC_RUNNER = None


__all__ = [
    "build_initial_state",
    "create_jarvis_graph",
    "get_or_compile_graph",
    "reset_compiled_graphs",
    "run_jarvis",
    "run_jarvis_async",
    "shutdown_sync_runner",
]
