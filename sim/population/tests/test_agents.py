"""Unit tests for PopulationManager and AgentEntity."""
from __future__ import annotations

from pathlib import Path

from sim.core.interfaces import ActorRole
from sim.population.agents import PopulationManager
from sim.population.behaviour import PopulationBehaviourModel
from sim.population.calibration import calibrate_from_csv
from sim.scheduler.rng import DeterministicRNG

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "paysim"


def test_population_manager_creation() -> None:
    params = calibrate_from_csv(DATA_DIR)
    root_rng = DeterministicRNG.from_seed(999)
    behaviour_model = PopulationBehaviourModel(params, root_rng=root_rng)

    manager = PopulationManager(behaviour_model, root_rng)
    agents = manager.create_population(num_users=30, num_merchants=5)

    assert len(manager.users) == 30
    assert len(manager.merchants) == 5
    assert len(agents) == 35

    for user in manager.users:
        assert user.role == ActorRole.USER
        assert user.initial_balance_paise >= 0
        assert len(user.linked_device_ids) == 1

    for merchant in manager.merchants:
        assert merchant.role == ActorRole.MERCHANT
        assert merchant.kyc_level == 3
        assert merchant.merchant_category_code is not None

    directory = manager.get_public_merchant_directory()
    assert len(directory) == 5
