"""Agent profiles and action parameter sampling.

Uses CalibratedParams and DeterministicRNG to sample realistic,
reproducible transaction parameters:
  - Action profile selection by frequency weighting
  - Repetition counts per discrete simulation step
  - Lognormal transaction amount sampling in paise
  - Piecewise initial balance sampling
  - Merchant category distribution sampling
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, cast

from sim.core.interfaces import TransactionType
from sim.population.interfaces import ActionProfile, CalibratedParams

if TYPE_CHECKING:
    from sim.scheduler.rng import DeterministicRNG


class ProfileSampler:
    """Samples action profiles, amounts, and initial entity states."""

    def __init__(self, params: CalibratedParams) -> None:
        self._params = params

    @property
    def params(self) -> CalibratedParams:
        return self._params

    def sample_profile(
        self, action_type: TransactionType, rng: DeterministicRNG
    ) -> ActionProfile:
        """Sample an ActionProfile for the given TransactionType based on frequency weights.

        Args:
            action_type: Type of transaction to sample.
            rng: DeterministicRNG instance.

        Returns:
            The selected ActionProfile.
        """
        profiles = self._params.profiles_by_type.get(action_type)
        if not profiles:
            raise ValueError(f"No calibrated profiles found for {action_type}")

        if len(profiles) == 1:
            return profiles[0]

        total_freq = sum(p.frequency for p in profiles)
        probabilities = [p.frequency / total_freq for p in profiles]
        selected = rng.choice(profiles, p=probabilities)
        return cast("ActionProfile", selected)

    def sample_repetition_count(
        self, profile: ActionProfile, rng: DeterministicRNG
    ) -> int:
        """Sample how many times an action should be repeated in a single burst.

        Args:
            profile: ActionProfile containing min_count and max_count.
            rng: DeterministicRNG instance.

        Returns:
            Integer count in [profile.min_count, profile.max_count].
        """
        if profile.min_count >= profile.max_count:
            return profile.min_count
        return int(rng.integers(profile.min_count, profile.max_count + 1))

    def sample_amount_paise(
        self, profile: ActionProfile, rng: DeterministicRNG
    ) -> int:
        """Sample transaction amount in integer paise using a lognormal distribution.

        The lognormal distribution parameters are derived from the empirical
        mean and standard deviation in the profile.

        Args:
            profile: ActionProfile containing avg_amount_paise and std_amount_paise.
            rng: DeterministicRNG instance.

        Returns:
            Amount in paise (minimum 100 paise = ₹1.00).
        """
        mu = float(profile.avg_amount_paise)
        sigma = float(profile.std_amount_paise)

        if mu <= 0 or sigma <= 0:
            return max(100, profile.avg_amount_paise)

        # Convert arithmetic mean and std to lognormal mu_ln and sigma_ln
        variance = sigma ** 2
        mu_squared = mu ** 2
        mu_ln = math.log(mu_squared / math.sqrt(mu_squared + variance))
        sigma_ln = math.sqrt(math.log(1.0 + (variance / mu_squared)))

        sampled = rng.lognormal(mu_ln, sigma_ln)
        amount_paise = int(round(sampled))
        return max(100, amount_paise)  # Minimum ₹1.00

    def sample_initial_balance(self, rng: DeterministicRNG) -> int:
        """Sample starting balance for a new entity from the piecewise distribution.

        Args:
            rng: DeterministicRNG instance.

        Returns:
            Initial balance in integer paise.
        """
        dist = self._params.initial_balance_distribution
        if not dist:
            return 100_000  # ₹1,000 fallback

        total_weight = sum(p for _, _, p in dist)
        probabilities = [p / total_weight for _, _, p in dist]

        # Choose the interval index
        indices = list(range(len(dist)))
        chosen = rng.choice(indices, p=probabilities)
        chosen_idx = int(str(chosen))
        min_b, max_b, _ = dist[chosen_idx]

        if min_b >= max_b:
            return min_b

        sampled = rng.uniform(float(min_b), float(max_b))
        return int(round(sampled))

    def sample_merchant_category(self, rng: DeterministicRNG) -> str:
        """Sample a Merchant Category Code (MCC).

        Args:
            rng: DeterministicRNG instance.

        Returns:
            MCC code string (e.g. "5411").
        """
        dist = self._params.merchant_category_distribution
        if not dist:
            return "5411"

        categories = list(dist.keys())
        total_p = sum(dist.values())
        probabilities = [p / total_p for p in dist.values()]

        return str(rng.choice(categories, p=probabilities))
