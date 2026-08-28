"""Unit tests for PopulationBehaviourModel and ProfileSampler."""
from __future__ import annotations

from pathlib import Path

from sim.core.interfaces import (
    TransactionType,
)
from sim.population.calibration import calibrate_from_csv
from sim.population.profiles import ProfileSampler
from sim.population.temporal import TemporalModel
from sim.scheduler.rng import DeterministicRNG

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "paysim"


def test_profile_sampler_amounts() -> None:
    params = calibrate_from_csv(DATA_DIR)
    sampler = ProfileSampler(params)
    rng = DeterministicRNG.from_seed(42)

    payment_profile = sampler.sample_profile(TransactionType.PAYMENT, rng)
    assert payment_profile.action_type == TransactionType.PAYMENT

    for _ in range(100):
        amount = sampler.sample_amount_paise(payment_profile, rng)
        assert isinstance(amount, int)
        assert amount >= 100

        rep_count = sampler.sample_repetition_count(payment_profile, rng)
        assert payment_profile.min_count <= rep_count <= payment_profile.max_count


def test_temporal_model_rates() -> None:
    params = calibrate_from_csv(DATA_DIR)
    temporal = TemporalModel(params)
    rng = DeterministicRNG.from_seed(42)

    # 12:00 PM on Monday = 12 hours in sim time
    sim_time_ns = 12.0 * 3600.0 * 1e9
    rate = temporal.get_rate(TransactionType.PAYMENT, sim_time_ns)
    assert rate > 0.0

    delta_t = temporal.sample_next_interarrival(TransactionType.PAYMENT, sim_time_ns, rng)
    assert delta_t >= 1_000_000.0  # >= 1ms
