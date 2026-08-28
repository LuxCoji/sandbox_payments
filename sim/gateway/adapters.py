"""LangGraph adapter — translates a LangGraph node call into a gateway
ToolGateway.call_tool() invocation.

Deliberately generic: this module lives in sim/gateway and must not import
anything from agents/ (see the "Sim does not import the red-team harness"
import-linter contract in pyproject.toml) — the graph-state shape a caller
uses is its own business. The only contract here is `state["action"]` being
a mapping with "tool_name" and "parameters" keys.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from sim.gateway.interfaces import ActorContext, ToolGateway, ToolResult


class LangGraphAdapter:
    """Adapter for using the ToolGateway with LangGraph agents."""

    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway

    def as_tool_node(self, context: ActorContext) -> Callable[[Mapping[str, object]], dict[str, ToolResult]]:
        """Build a LangGraph node function bound to one ActorContext.

        The returned function reads `state["action"]` (expected shape:
        `{"tool_name": str, "parameters": dict[str, object]}`) and calls
        `call_tool()`, returning `{"tool_result": ToolResult}` as a partial
        state update — the LangGraph convention of a node returning only
        the keys it changes.
        """

        def node(state: Mapping[str, object]) -> dict[str, ToolResult]:
            action = state["action"]
            if not isinstance(action, Mapping):
                raise TypeError(f"state['action'] must be a mapping with tool_name/parameters, got {type(action)}")
            tool_name = action["tool_name"]
            parameters = action.get("parameters", {})
            result = self._gateway.call_tool(tool_name, parameters, context)
            return {"tool_result": result}

        return node
