"""Pass-through selector: expose every registered tool.

Preserves the pre-selection-layer behaviour (the model sees the full
catalogue each turn). Useful as a baseline or fallback when no narrowing
is desired.
"""

from typing import Any, Dict, List, Optional

from agents.agent_api.app.tools.base import ToolRegistry


class StaticToolSelector:
    """Expose every registered tool on every turn."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    @property
    def decision(self) -> None:
        return None

    def select_schemas(
        self,
        query: str,
        registry: ToolRegistry,
        active_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        del query, active_domains
        return registry.openai_schemas()
