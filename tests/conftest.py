"""Shared pytest configuration for the Jarvis test suite.

Native LangSmith tracing is governed by the LANGSMITH_TRACING env var. A local
``.env`` may enable it for real runs, but the test suite must never emit
telemetry or open production checkpoint connections. Disable tracing and force
in-memory checkpointing for the whole session. This mirrors how per-run file
logs are auto-disabled under pytest.
"""

import asyncio
import os
import tempfile

import pytest

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["JARVIS_CHECKPOINT_BACKEND"] = "memory"
os.environ["JARVIS_POSTGRES_DSN"] = ""
os.environ["DATABASE_URL"] = ""

# Pin the retained legacy token-file fallback to a guaranteed-absent path so a
# developer's real ``token.json`` cannot leak into credential-loading tests.
os.environ["GOOGLE_TOKEN_PATH"] = os.path.join(
    tempfile.gettempdir(), "jarvis-tests-no-such-dir", "absent-token.json"
)


@pytest.fixture(autouse=True)
def _reset_shared_runtime_resources():
    """Keep process-wide clients and compiled graphs isolated between tests."""

    from agents.agent_api.app.graph import builder
    from agents.agent_api.app.graph.nodes import orchestrator, summarize
    from agents.agent_api.app.router import client as router_client

    def reset() -> None:
        orchestrator.close_shared_agent_client()
        asyncio.run(orchestrator.close_shared_async_agent_client())
        router_client.close_shared_router_client()
        asyncio.run(router_client.close_shared_async_router_openai_client())
        summarize.close_shared_summarizer_client()
        asyncio.run(summarize.close_shared_async_summarizer_client())
        builder.reset_compiled_graphs()

    reset()
    yield
    reset()
