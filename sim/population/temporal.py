"""Temporal rate modeling and Poisson inter-arrival sampling.

Uses a 24x7 empirical rate matrix from PaySim to sample realistic,
non-homogeneous Poisson process inter-arrival times across diurnal
and weekly cycles.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.core.interfaces import TransactionType
    from sim.population.interfaces import CalibratedParams
    from sim.scheduler.rng import DeterministicRNG

# Time constants in nanoseconds
NS_PER_SECOND: float = 1_000_000_000.0
NS_PER_HOUR: float = 3_600.0 * NS_PER_SECOND
NS_PER_DAY: float = 24.0 * NS_PER_HOUR
NS_PER_WEEK: float = 7.0 * NS_PER_DAY
MIN_INTERARRIVAL_NS: float = 1_000_000.0  # 1 millisecond lower bound


class TemporalModel:
    """Computes time-of-day/day-of-week rates and samples discrete event delays."""

    def __init__(self, params: CalibratedParams) -> None:
        self._params = params

    @staticmethod
    def get_day_and_hour(sim_time_ns: float) -> tuple[int, int]:
        """Convert simulation nanoseconds to (day_of_week, hour_of_day).

        Args:
            sim_time_ns: Current simulation timestamp in nanoseconds.

        Returns:
            Tuple of (day_of_week: 0..6 for Mon..Sun, hour_of_day: 0..23).
        """
        if sim_time_ns < 0:
            sim_time_ns = 0.0

        total_hours = int(sim_time_ns // NS_PER_HOUR)
        day_of_week = (total_hours // 24) % 7
        hour_of_day = total_hours % 24
        return day_of_week, hour_of_day

    def get_rate(self, action_type: TransactionType, sim_time_ns: float) -> float:
        """Get the calibrated intensity rate (actions/hour) for the given time.

        Args:
            action_type: TransactionType.
            sim_time_ns: Simulation timestamp in nanoseconds.

        Returns:
            Rate in transactions per hour.
        """
        matrix = self._params.temporal_rate_matrix.get(action_type)
        if not matrix:
            return 1.0  # Baseline fallback

        day, hour = self.get_day_and_hour(sim_time_ns)
        try:
            rate = matrix[day][hour]
            return max(0.01, float(rate))
        except (IndexError, TypeError):
            return 1.0

    def sample_next_interarrival(
        self,
        action_type: TransactionType,
        current_time_ns: float,
        rng: DeterministicRNG,
        population_size: int = 1,
    ) -> float:
        """Sample the delta time (in nanoseconds) until the next action occurs.

        Uses the Lewis-Shedler thinning algorithm for non-homogeneous Poisson processes.

        Args:
            action_type: Type of action scheduled.
            current_time_ns: Current simulation timestamp.
            rng: DeterministicRNG instance.
            population_size: Number of active agents to distribute the global rate across.

        Returns:
            Delta time in nanoseconds until next event (float).
        """
        # Find the maximum rate for thinning (upper bound lambda)
        matrix = self._params.temporal_rate_matrix.get(action_type)
        max_rate = 1.0 if not matrix else max(max(day) for day in matrix)

        max_rate_per_agent = max(0.01, max_rate) / max(1, population_size)
        lambda_star = max_rate_per_agent
        mean_delta_star_ns = NS_PER_HOUR / lambda_star

        t_ns = current_time_ns
        while True:
            # Sample exponential step based on lambda_star
            step_ns = rng.exponential(scale=mean_delta_star_ns)
            t_ns += step_ns

            # Compute actual rate at time t_ns
            current_rate = self.get_rate(action_type, t_ns)
            actual_rate_per_agent = current_rate / max(1, population_size)

            # Accept with probability lambda(t) / lambda_star
            if rng.random() < (actual_rate_per_agent / lambda_star):
                delta_t_ns = t_ns - current_time_ns
                return max(MIN_INTERARRIVAL_NS, float(delta_t_ns))
