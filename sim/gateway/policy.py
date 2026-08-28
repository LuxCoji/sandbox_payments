"""Policy enforcement: capability checks + rate limiting."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.gateway.interfaces import ActorContext, Capability

NANOS_PER_DAY = 86_400_000_000_000

# Additional tier-wide caps, enforced alongside (not instead of) each tool's
# own rate_limit_per_step/rate_limit_per_day. Keyed by ToolSpec.rate_limit_tier.
# "branch_op" covers fork/checkout/diff/commit_strategy — real ChronoDAG work,
# not comparable in cost to a payment call. See docs/redteam_agent_design.md §3.
TIER_LIMITS: dict[str, tuple[int | None, int | None]] = {
    "normal": (None, None),
    "branch_op": (2, 10),
}


class RateLimiter:
    """Simple per-actor per-tool counters, plus a second per-actor per-tier
    counter dimension (see TIER_LIMITS) for cost classes wider than one tool.
    """

    def __init__(self) -> None:
        self._step_counters: dict[tuple[str, str], int] = {}
        self._day_counters: dict[tuple[str, str], int] = {}
        self._tier_step_counters: dict[tuple[str, str], int] = {}
        self._tier_day_counters: dict[tuple[str, str], int] = {}
        self._current_step_time: int = -1
        self._current_day: int = -1

    def _roll_windows(self, sim_time_ns: float) -> None:
        current_step = int(sim_time_ns)
        if current_step != self._current_step_time:
            self._step_counters.clear()
            self._tier_step_counters.clear()
            self._current_step_time = current_step

        current_day = int(sim_time_ns // NANOS_PER_DAY)
        if current_day != self._current_day:
            self._day_counters.clear()
            self._tier_day_counters.clear()
            self._current_day = current_day

    @staticmethod
    def _check_and_increment_key(
        step_counters: dict[tuple[str, str], int],
        day_counters: dict[tuple[str, str], int],
        key: tuple[str, str],
        limit_per_step: int | None,
        limit_per_day: int | None,
    ) -> str | None:
        step_count = step_counters.get(key, 0)
        day_count = day_counters.get(key, 0)

        if limit_per_step is not None and step_count >= limit_per_step:
            return f"Rate limit exceeded: {limit_per_step} per step"

        if limit_per_day is not None and day_count >= limit_per_day:
            return f"Rate limit exceeded: {limit_per_day} per day"

        if limit_per_step is not None:
            step_counters[key] = step_count + 1

        if limit_per_day is not None:
            day_counters[key] = day_count + 1

        return None

    def check_and_increment(
        self, actor_id: str, tool_name: str, sim_time_ns: float,
        limit_per_step: int | None, limit_per_day: int | None,
    ) -> str | None:
        self._roll_windows(sim_time_ns)
        return self._check_and_increment_key(
            self._step_counters, self._day_counters, (actor_id, tool_name),
            limit_per_step, limit_per_day,
        )

    def check_and_increment_tier(self, actor_id: str, tier: str, sim_time_ns: float) -> str | None:
        """Tier-wide check, independent of and in addition to the per-tool
        counters above — a separate counter dimension keyed (actor_id, tier)
        rather than (actor_id, tool_name), so e.g. fork_branch and
        commit_strategy share one "branch_op" budget regardless of which
        specific tool was called.
        """
        self._roll_windows(sim_time_ns)
        limit_per_step, limit_per_day = TIER_LIMITS.get(tier, (None, None))
        return self._check_and_increment_key(
            self._tier_step_counters, self._tier_day_counters, (actor_id, tier),
            limit_per_step, limit_per_day,
        )


def check_capabilities(
    context: ActorContext, required: frozenset[Capability]
) -> str | None:
    if not required.issubset(context.capabilities):
        missing = required - context.capabilities
        return f"Missing required capabilities: {missing}"
    return None
