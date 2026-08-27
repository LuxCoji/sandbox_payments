"""Shared pytest fixtures."""
import pytest

from sim.core.engine import WorldEngineImpl
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG


@pytest.fixture
def rng() -> DeterministicRNG:
    return DeterministicRNG.from_seed(42)

@pytest.fixture
def env() -> SimulationEnv:
    return SimulationEnv()

@pytest.fixture
def engine(env: SimulationEnv, rng: DeterministicRNG) -> WorldEngineImpl:
    return WorldEngineImpl(env=env, rng=rng)
