"""Contract tests — WorldEngine protocol conformance."""
from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from sim.core.engine import WorldEngineImpl
from sim.core.events import AccountCreated
from sim.core.interfaces import AccountType, ActorRole


@given(seed=st.integers(0, 2**32 - 1))
def test_engine_hash_determinism(seed: int) -> None:
    from sim.scheduler.env import SimulationEnv
    from sim.scheduler.rng import DeterministicRNG

    env = SimulationEnv()
    rng = DeterministicRNG.from_seed(42)
    engine1 = WorldEngineImpl(env, rng, branch_id="main")

    env2 = SimulationEnv()
    rng2 = DeterministicRNG.from_seed(42)
    engine2 = WorldEngineImpl(env2, rng2, branch_id="main")

    e1 = AccountCreated(event_id="e1", event_type="AccountCreated", sim_time_ns=0, actor_id="sys", branch_id="main", seq_num=1, account_id="acc1", account_type=AccountType.PERSONAL, initial_balance_paise=1000, kyc_level=1, owner_id="user1")
    engine1._apply_event(e1)
    engine2._apply_event(e1)

    assert engine1.get_state_hash() == engine2.get_state_hash()


def test_world_view_immutability(engine: WorldEngineImpl) -> None:
    e1 = AccountCreated(event_id="e1", event_type="AccountCreated", sim_time_ns=0, actor_id="sys", branch_id="main", seq_num=1, account_id="acc1", account_type=AccountType.PERSONAL, initial_balance_paise=1000, kyc_level=1, owner_id="user1")
    engine._apply_event(e1)

    view = engine.get_world_view("user1", ActorRole.USER)
    # Dataclasses are frozen, attempting to modify should raise exception
    from dataclasses import FrozenInstanceError

    import pytest

    with pytest.raises(FrozenInstanceError):
        view.sim_time_ns = 1.0  # type: ignore

    with pytest.raises(FrozenInstanceError):
        view.accounts[0].balance_paise = 0  # type: ignore
