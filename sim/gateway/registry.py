"""ToolRegistry — register and lookup simulation tools."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from sim.gateway.interfaces import ActorContext, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Callable] = {}

    def register_tool(self, spec: ToolSpec, handler: Callable) -> None:
        self._tools[spec.name] = spec
        self._handlers[spec.name] = handler

    def get_tool(self, name: str) -> tuple[ToolSpec, Callable] | None:
        spec = self._tools.get(name)
        handler = self._handlers.get(name)
        if spec and handler:
            return spec, handler
        return None

    def list_tools(self, context: ActorContext) -> list[ToolSpec]:
        """Return tools whose required_capabilities are a subset of context.capabilities."""
        return [
            spec for spec in self._tools.values()
            if spec.required_capabilities.issubset(context.capabilities)
        ]
