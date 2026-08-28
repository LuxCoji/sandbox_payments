"""Policy enforcement: capability checks + rate limiting."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.gateway.interfaces import ActorContext, Capability

NANOS_PER_DAY = 86_400_000_000_000


class RateLimiter:
    """Simple per-actor per-tool counters."""

    def __init__(self) -> None:
        self._step_counters: dict[tuple[str, str], int] = {}
        self._day_counters: dict[tuple[str, str], int] = {}
        self._current_step_time: int = -1
        self._current_day: int = -1

    def check_and_increment(
        self, actor_id: str, tool_name: str, sim_time_ns: float,
        limit_per_step: int | None, limit_per_day: int | None,
    ) -> str | None:

        current_step = int(sim_time_ns)
        if current_step != self._current_step_time:
            self._step_counters.clear()
            self._current_step_time = current_step

        current_day = int(sim_time_ns // NANOS_PER_DAY)
        if current_day != self._current_day:
            self._day_counters.clear()
            self._current_day = current_day

        key = (actor_id, tool_name)
        step_count = self._step_counters.get(key, 0)
        day_count = self._day_counters.get(key, 0)

        if limit_per_step is not None and step_count >= limit_per_step:
            return f"Rate limit exceeded: {limit_per_step} per step"

        if limit_per_day is not None and day_count >= limit_per_day:
            return f"Rate limit exceeded: {limit_per_day} per day"

        if limit_per_step is not None:
            self._step_counters[key] = step_count + 1

        if limit_per_day is not None:
            self._day_counters[key] = day_count + 1

        return None


def check_capabilities(
    context: ActorContext, required: frozenset[Capability]
) -> str | None:
    if not required.issubset(context.capabilities):
        missing = required - context.capabilities
        return f"Missing required capabilities: {missing}"
    return None
