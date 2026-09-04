"""Pluggable tool selectors.

Each module in this package provides one ToolSelector implementation.
The active selector is chosen via ``selection.get_selector(name)``.
"""

from agents.agent_api.app.tools.selectors.keyword import KeywordToolSelector
from agents.agent_api.app.tools.selectors.static import StaticToolSelector

__all__ = ["KeywordToolSelector", "StaticToolSelector"]
