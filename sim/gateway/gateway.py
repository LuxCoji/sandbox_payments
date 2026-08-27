"""ToolGateway implementation — capability-gated, rate-limited tool execution."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sim.gateway.interfaces import ActorContext, ToolResult, ToolSpec
from sim.gateway.policy import RateLimiter, check_capabilities

if TYPE_CHECKING:
    from sim.core.interfaces import ActorRole, WorldEngine
    from sim.gateway.registry import ToolRegistry


class ToolGatewayImpl:
    """Concrete implementation of the ToolGateway protocol."""

    def __init__(self, registry: ToolRegistry, rate_limiter: RateLimiter, engine: WorldEngine) -> None:
        self._registry = registry
        self._rate_limiter = rate_limiter
        self._engine = engine

    def register_tool(self, spec: ToolSpec, handler: Any) -> None:
        self._registry.register_tool(spec, handler)

    def list_tools(self, context: ActorContext) -> list[ToolSpec]:
        return self._registry.list_tools(context)

    def call_tool(
        self,
        tool_name: str,
        parameters: dict[str, object],
        context: ActorContext,
    ) -> ToolResult:

        tool_data = self._registry.get_tool(tool_name)
        if not tool_data:
            return ToolResult(
                success=False, tool_name=tool_name, error_code="TOOL_NOT_FOUND",
                error_message=f"Tool {tool_name} not found"
            )

        spec, handler = tool_data

        # Check capabilities
        cap_err = check_capabilities(context, spec.required_capabilities)
        if cap_err:
            return ToolResult(
                success=False, tool_name=tool_name, error_code="UNAUTHORIZED",
                error_message=cap_err
            )

        # Check rate limits
        rl_err = self._rate_limiter.check_and_increment(
            context.actor_id, tool_name, self._engine.sim_time_ns,
            spec.rate_limit_per_step, spec.rate_limit_per_day
        )
        if rl_err:
            return ToolResult(
                success=False, tool_name=tool_name, error_code="RATE_LIMITED",
                error_message=rl_err
            )

        try:
            # Handler builds command, calls engine, returns events
            events = handler(context, parameters, self._engine)

            # For simplicity, returning events directly in data
            raw_data = {"events": [e.__dict__ for e in events]}

            # Filter output fields
            filtered_data, removed = self._filter_fields(
                raw_data, context.actor_role, spec.visible_fields.get(context.actor_role)
            )

            return ToolResult(
                success=True, tool_name=tool_name, data=filtered_data,
                filtered_fields=removed
            )

        except Exception as e:
            return ToolResult(
                success=False, tool_name=tool_name, error_code="INTERNAL_ERROR",
                error_message=str(e)
            )

    def _filter_fields(
        self, data: dict, role: ActorRole, visible: frozenset[str] | None
    ) -> tuple[dict, tuple[str, ...]]:
        if visible is None:
            return data, ()

        filtered = {}
        removed = []
        for k, v in data.items():
            if k in visible:
                filtered[k] = v
            else:
                removed.append(k)

        return filtered, tuple(removed)
