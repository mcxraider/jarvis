"""Query router: a lightweight pre-orchestrator domain classifier.

Opt-in and non-critical. Given a raw query and the runtime snapshot, the router
decides which service domains the request needs so the orchestrator can see a
slimmer prompt and fewer tool schemas. Any failure degrades to today's behavior
(all domains, static tool selection).

Public surface:
- ``RouterDecision`` / ``build_router_messages`` — schema + prompt (``prompt.py``)
- ``RouterClient`` / ``RouterClientError`` — the sync classifier call (``client.py``)
"""

from agents.agent_api.app.router.model_router import (
    ModelRouter,
    ModelSelection,
    create_default_model_router,
)
from agents.agent_api.app.router.prompt import (
    RouterDecision,
    RouterDomain,
    RouterOutcome,
    build_router_messages,
    build_router_system_prompt,
)

__all__ = [
    "ModelRouter",
    "ModelSelection",
    "RouterClient",
    "RouterClientError",
    "RouterDecision",
    "RouterDomain",
    "RouterOutcome",
    "build_router_messages",
    "build_router_system_prompt",
    "create_default_model_router",
]


def __getattr__(name: str):
    if name in {"RouterClient", "RouterClientError"}:
        from agents.agent_api.app.router.client import RouterClient, RouterClientError

        return {"RouterClient": RouterClient, "RouterClientError": RouterClientError}[name]
    raise AttributeError(name)
