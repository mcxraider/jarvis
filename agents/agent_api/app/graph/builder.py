"""LangGraph builder: graph factory, initial state, and the run entrypoint."""

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from langgraph.types import Command

from agents.agent_api.app.checkpointing import DEFAULT_CHECKPOINTER
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
    DeepSeekAgentClient,
    UsageSummary,
    create_agent_node,
)
from agents.agent_api.app.graph.nodes.prepare_confirm import create_prepare_confirm_node
from agents.agent_api.app.graph.nodes.summarize import create_summarize_node
from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.graph.nodes.validate_entities import create_validate_entities_node
from agents.agent_api.app.graph.prompts import USER_PROMPT, build_initial_messages
from agents.agent_api.app.graph.state import JarvisState, enrich_interrupt_status
from agents.agent_api.app.idempotency import DEFAULT_IDEMPOTENCY_STORE, IdempotencyStore
from agents.agent_api.app.run_logging import (
    FileLoggingTracer,
    RunLogIdentity,
    format_singapore_log_iso,
    open_run_log,
)
from agents.agent_api.app.tools.dispatcher import ToolDispatcher
from agents.agent_api.app.tools.registry_factory import build_default_registry
from agents.agent_api.app.tools.selection import ToolSelector, get_selector
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


_builder_logger = logging.getLogger(__name__)


def _register_thread(
    thread_id: str,
    telegram_user_id: Optional[int],
    user_prompt: str,
    status: str,
    resuming: bool,
) -> None:
    """Upsert thread metadata. Fire-and-forget — never crashes the request."""
    if telegram_user_id is None:
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
                        INSERT INTO threads (thread_id, user_id, title, status, message_count)
                        SELECT %s, u.id, LEFT(%s, 100), %s, 1
                        FROM users u
                        WHERE u.telegram_user_id = %s
                        ON CONFLICT (thread_id) DO UPDATE
                        SET message_count = threads.message_count + 1,
                            status = EXCLUDED.status,
                            updated_at = NOW()
                        """,
                        (thread_id, user_prompt, status, str(telegram_user_id)),
                    )
    except Exception as exc:
        _builder_logger.warning(
            "Thread registration failed (non-fatal).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )


def _log_usage(
    telegram_user_id: Optional[int],
    thread_id: str,
    usage: "UsageSummary",
    latency_ms: int,
    model: str,
) -> None:
    """Write usage telemetry to Supabase. Fire-and-forget."""
    if telegram_user_id is None or not usage.total_tokens:
        return
    try:
        from agents.agent_api.app.db import get_pool

        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO usage_logs (user_id, thread_id, event_type, model,
                                           input_tokens, output_tokens, latency_ms)
                    SELECT u.id, %s, 'run', %s, %s, %s, %s
                    FROM users u
                    WHERE u.telegram_user_id = %s
                    """,
                    (
                        thread_id,
                        model,
                        usage.prompt_tokens or 0,
                        usage.completion_tokens or 0,
                        latency_ms,
                        str(telegram_user_id),
                    ),
                )
    except Exception as exc:
        _builder_logger.warning(
            "Usage logging failed (non-fatal).",
            extra={"thread_id": thread_id, "error": type(exc).__name__},
        )


def create_jarvis_graph(
    agent_client: Any,
    tool_dispatcher: ToolDispatcher,
    max_agent_turns: int = MAX_AGENT_TURNS,
    tracer: Optional[TracePrinter] = None,
    checkpointer: Optional[Any] = None,
    tool_selector: Optional[ToolSelector] = None,
):
    """Create the Jarvis LangGraph app from declarative node specs.

    The tool catalogue the agent sees comes from ``tool_dispatcher.registry``, so
    new tool domains plug in via the registry and new nodes/stages via NodeSpec —
    neither requires editing this function's body beyond the spec list.
    ``tool_selector`` decides which of those tools the model sees each turn
    (default: the whole catalogue; see ``tools/selection.py``).
    """

    tracer = tracer or NULL_TRACE
    checkpointer = checkpointer or DEFAULT_CHECKPOINTER
    registry = tool_dispatcher.registry

    node_specs = [
        NodeSpec(
            name="agent",
            node=create_agent_node(
                agent_client,
                registry,
                max_agent_turns,
                tracer,
                tool_selector=tool_selector,
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
                "confirm": "prepare_confirm",
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


def build_initial_state(
    user_prompt: str,
    user_id: str = USER_ID,
    thread_id: Optional[str] = None,
    request_source: str = "api",
    timezone: Optional[str] = None,
) -> JarvisState:
    """Create a fresh state object for one Jarvis run."""

    thread_id = thread_id or str(uuid.uuid4())
    return {
        "messages": build_initial_messages(user_prompt, timezone=timezone),
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
    }


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
    telegram_user_id: Optional[int] = None,
    telegram_username: Optional[str] = None,
    telegram_first_name: Optional[str] = None,
    clarification_reply: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    request_id: Optional[str] = None,
    tool_selector: Optional[ToolSelector] = None,
    idempotency_store: Optional[IdempotencyStore] = None,
) -> JarvisState:
    """Run the full Jarvis graph for one invocation.

    Each call is one LangSmith trace named ``jarvis.invoke`` or ``jarvis.resume``,
    correlated by ``request_id`` and grouped by ``thread_id``. ``request_id`` is
    generated when callers omit it so every run is traceable.
    """

    if clarification_reply is not None and not thread_id:
        raise ValueError("thread_id is required when resuming with clarification_reply.")
    thread_id = thread_id or str(uuid.uuid4())
    request_id = request_id or str(uuid.uuid4())
    checkpointer = checkpointer or DEFAULT_CHECKPOINTER
    tool_selector = tool_selector or get_selector("static", allow_mutations=allow_mutations)
    if idempotency_store is None:
        idempotency_store = DEFAULT_IDEMPOTENCY_STORE

    resuming = clarification_reply is not None
    invocation_type = "resume" if resuming else "invoke"
    run_name = f"jarvis.{invocation_type}"
    started_at = datetime.now()

    base_tracer = tracer if tracer is not None else TracePrinter()
    run_log_identity = RunLogIdentity(
        request_source=request_source,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        telegram_first_name=telegram_first_name,
    )
    run_log = open_run_log(thread_id, run_log_identity)
    if run_log is not None:
        run_log.write_header(
            started_at=format_singapore_log_iso(started_at),
            request_id=request_id,
            thread_id=thread_id,
            user_id=user_id,
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
    tracer.payload("runtime.prompt", "user_prompt", user_prompt)

    agent_client = agent_client or DeepSeekAgentClient(tracer=tracer)
    todoist_client = todoist_client or TodoistApiClient(
        tracer=tracer,
        telegram_user_id=telegram_user_id,
    )

    # Caller-provided clients (e.g. the CLI runner) are built before run_jarvis
    # wraps the tracer, so retarget them at this run's tracer to keep their
    # agent.* / todoist.* events flowing into the per-run file log.
    for client in (agent_client, todoist_client):
        if getattr(client, "tracer", None) is not None:
            client.tracer = tracer
    registry = build_default_registry(todoist_client)
    dispatcher = ToolDispatcher(
        registry,
        allow_mutations=allow_mutations,
        tracer=tracer,
        idempotency_store=idempotency_store,
        idempotency_operation_ttl_seconds=settings.idempotency_operation_ttl_seconds,
        idempotency_lease_seconds=settings.idempotency_lease_seconds,
        idempotency_wait_seconds=settings.idempotency_wait_seconds,
        idempotency_poll_interval_seconds=settings.idempotency_poll_interval_seconds,
    )
    app = create_jarvis_graph(
        agent_client,
        dispatcher,
        max_agent_turns=max_agent_turns,
        tracer=tracer,
        checkpointer=checkpointer,
        tool_selector=tool_selector,
    )
    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": run_name,
        "tags": [*LANGSMITH_TAGS, invocation_type],
        "metadata": {
            "request_id": request_id,
            "thread_id": thread_id,
            "invocation_type": invocation_type,
            "user_id": user_id,
            "telegram_user_id": telegram_user_id,
            "telegram_username": telegram_username,
            "telegram_first_name": telegram_first_name,
            "request_source": request_source,
            "model": DEEPSEEK_MODEL,
            "allow_mutations": allow_mutations,
            "max_agent_turns": max_agent_turns,
        },
    }
    tracer.event(
        "runtime.graph", "Compiled graph.", nodes="agent, validate_entities, hitl, tools"
    )
    # Native LangSmith tracing (LangGraph node spans + @traceable / wrap_openai
    # child spans) is governed by the LANGSMITH_TRACING env var. Tracing is
    # best-effort: callback failures never propagate into the graph result.
    if resuming:
        result = app.invoke(Command(resume=clarification_reply), config)
    else:
        user_timezone: Optional[str] = None
        if telegram_user_id is not None:
            from agents.agent_api.app.credentials import get_user_preferences

            prefs = get_user_preferences(telegram_user_id)
            user_timezone = prefs.get("timezone")
        result = app.invoke(
            build_initial_state(
                user_prompt,
                user_id=user_id,
                thread_id=thread_id,
                request_source=request_source,
                timezone=user_timezone,
            ),
            config,
        )
    result = enrich_interrupt_status(result, thread_id)

    thread_status = "interrupted" if result.get("interrupted") else "completed"
    _register_thread(thread_id, telegram_user_id, user_prompt, thread_status, resuming)

    usage: UsageSummary = getattr(agent_client, "usage", None) or UsageSummary()
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
        result["run_log_path"] = str(run_log.path.resolve())

    duration_ms = int((finished_at - started_at).total_seconds() * 1000)
    _log_usage(telegram_user_id, thread_id, usage, duration_ms, DEEPSEEK_MODEL)

    return result


__all__ = ["build_initial_state", "create_jarvis_graph", "run_jarvis"]
