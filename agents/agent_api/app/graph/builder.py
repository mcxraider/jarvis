"""LangGraph builder: graph factory, initial state, and the run entrypoint."""

import uuid
from datetime import datetime
from typing import Any, Optional

from langgraph.types import Command

from agents.agent_api.app.checkpointing import DEFAULT_CHECKPOINTER
from agents.agent_api.app.constants import (
    ALLOW_MUTATIONS,
    DEEPSEEK_MODEL,
    LANGSMITH_TAGS,
    MAX_AGENT_TURNS,
    USER_ID,
)
from agents.agent_api.app.graph.assembly import NodeSpec, build_graph
from agents.agent_api.app.graph.edges import route_after_agent
from agents.agent_api.app.graph.nodes.hitl import create_hitl_node
from agents.agent_api.app.graph.nodes.orchestrator import (
    DeepSeekAgentClient,
    UsageSummary,
    create_agent_node,
)
from agents.agent_api.app.graph.nodes.tools import create_tools_node
from agents.agent_api.app.graph.prompts import USER_PROMPT, build_initial_messages
from agents.agent_api.app.graph.state import JarvisState, enrich_interrupt_status
from agents.agent_api.app.run_logging import FileLoggingTracer, open_run_log
from agents.agent_api.app.tools.dispatcher import ToolDispatcher
from agents.agent_api.app.tools.registry_factory import build_default_registry
from agents.agent_api.app.tools.selection import DEFAULT_TOOL_SELECTOR, ToolSelector
from agents.agent_api.app.tools.todoist.client import TodoistApiClient
from agents.agent_api.app.tracing import NULL_TRACE, TracePrinter


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
            # After the model speaks: ask, execute tools, or stop.
            router=route_after_agent,
            route_map={"hitl": "hitl", "tools": "tools", "end": "end"},
        ),
        # Tool/clarification observations always return to the model.
        NodeSpec(name="tools", node=create_tools_node(tool_dispatcher, tracer), static_route="agent"),
        NodeSpec(name="hitl", node=create_hitl_node(tracer), static_route="agent"),
    ]

    return build_graph(JarvisState, node_specs, entry="agent", checkpointer=checkpointer)


def build_initial_state(
    user_prompt: str,
    user_id: str = USER_ID,
    thread_id: Optional[str] = None,
    request_source: str = "api",
) -> JarvisState:
    """Create a fresh state object for one Jarvis run."""

    thread_id = thread_id or str(uuid.uuid4())
    return {
        "messages": build_initial_messages(user_prompt),
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
    clarification_reply: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    request_id: Optional[str] = None,
    tool_selector: Optional[ToolSelector] = None,
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
    tool_selector = tool_selector or DEFAULT_TOOL_SELECTOR

    resuming = clarification_reply is not None
    invocation_type = "resume" if resuming else "invoke"
    run_name = f"jarvis.{invocation_type}"
    started_at = datetime.now()

    base_tracer = tracer if tracer is not None else TracePrinter()
    run_log = open_run_log(thread_id)
    if run_log is not None:
        run_log.write_header(
            started_at=started_at.isoformat(timespec="seconds"),
            request_id=request_id,
            thread_id=thread_id,
            user_id=user_id,
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
    todoist_client = todoist_client or TodoistApiClient(tracer=tracer)
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
            "request_source": request_source,
            "model": DEEPSEEK_MODEL,
            "allow_mutations": allow_mutations,
            "max_agent_turns": max_agent_turns,
        },
    }
    tracer.event("runtime.graph", "Compiled graph.", nodes="agent, hitl, tools")
    # Native LangSmith tracing (LangGraph node spans + @traceable / wrap_openai
    # child spans) is governed by the LANGSMITH_TRACING env var. Tracing is
    # best-effort: callback failures never propagate into the graph result.
    if resuming:
        result = app.invoke(Command(resume=clarification_reply), config)
    else:
        result = app.invoke(
            build_initial_state(
                user_prompt,
                user_id=user_id,
                thread_id=thread_id,
                request_source=request_source,
            ),
            config,
        )
    result = enrich_interrupt_status(result, thread_id)

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
        total_tokens=usage.total_tokens or None,
    )
    if run_log is not None:
        run_log.write_footer(
            finished_at=finished_at.isoformat(timespec="seconds"),
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
            reasoning_tokens=usage.reasoning_tokens,
        )
    return result


__all__ = ["build_initial_state", "create_jarvis_graph", "run_jarvis"]
