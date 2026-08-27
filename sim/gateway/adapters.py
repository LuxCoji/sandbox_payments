"""LangGraph adapter — translates agent states to tool calls."""
from __future__ import annotations


class LangGraphAdapter:
    """Adapter for using the ToolGateway with LangGraph agents.

    Stub implementation for MVP.
    """

    def __init__(self, gateway: object) -> None:
        self._gateway = gateway

    def as_tool_node(self) -> dict[str, object]:
        raise NotImplementedError("LangGraph integration pending")
