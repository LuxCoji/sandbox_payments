"""Policy enforcement: capability checks + rate limiting."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.gateway.interfaces import ActorContext, Capability

NANOS_PER_DAY = 86_400_000_000_000


class RateLimiter:
    """Simple per-actor per-tool counters."""

    def __init__(self) -> None:
        # (actor_id, tool_name, period) -> count
        self._counters: dict[tuple[str, str, int], int] = {}

    def check_and_increment(
        self, actor_id: str, tool_name: str, sim_time_ns: float,
        limit_per_step: int | None, limit_per_day: int | None,
    ) -> str | None:

        if limit_per_step is not None:
            step_key = (actor_id, tool_name, int(sim_time_ns))
            count = self._counters.get(step_key, 0)
            if count >= limit_per_step:
                return f"Rate limit exceeded: {limit_per_step} per step"
            self._counters[step_key] = count + 1

        if limit_per_day is not None:
            day_key = (actor_id, tool_name, int(sim_time_ns // NANOS_PER_DAY))
            count = self._counters.get(day_key, 0)
            if count >= limit_per_day:
                return f"Rate limit exceeded: {limit_per_day} per day"
            self._counters[day_key] = count + 1

        return None


def check_capabilities(
    context: ActorContext, required: frozenset[Capability]
) -> str | None:
    if not required.issubset(context.capabilities):
        missing = required - context.capabilities
        return f"Missing required capabilities: {missing}"
    return None
