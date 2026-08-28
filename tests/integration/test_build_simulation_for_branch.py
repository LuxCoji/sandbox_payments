"""build_simulation_for_branch() round-trip — the mechanism a red-team
session uses to run on a forked branch (docs/redteam_agent_design.md §3/§6
Phase 3). Uses InMemoryChronoDAG (monkeypatched in place of
sim.main.PostgresChronoDAG) rather than a real Postgres connection, per the
existing pattern in test_determinism.py.
"""
from __future__ import annotations

import pytest

from sim.chrono.tests._fake_dag import InMemoryChronoDAG
from sim.config import SimConfig
from sim.core.interfaces import AccountType, ActorRole
from sim.main import build_simulation, build_simulation_for_branch


def test_build_simulation_for_branch_round_trips_state_and_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same InMemoryChronoDAG instance returned on every call — build_simulation()
    # and build_simulation_for_branch() each construct their own
    # PostgresChronoDAG(db_url) internally, so both need to land on the same
    # underlying store for the fork/checkout round trip to see real data.
    shared_dag = InMemoryChronoDAG()
    monkeypatch.setattr("sim.main.PostgresChronoDAG", lambda db_url=None: shared_dag)

    config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    engine, _gateway, chrono = build_simulation(config)

    engine.create_account(
        account_id="acc-1", owner_id="user-1",
        account_type=AccountType.PERSONAL, initial_balance_paise=10_000, kyc_level=1,
    )
    engine.create_account(
        account_id="acc-2", owner_id="user-2",
        account_type=AccountType.PERSONAL, initial_balance_paise=0, kyc_level=1,
    )
    original_hash = engine.get_state_hash()

    checkpoint = chrono.create_checkpoint(
        branch_id="main",
        event_number=engine._seq_num,
        sim_time_ns=engine.sim_time_ns,
        state_hash=original_hash,
        aggregate_snapshot=engine.get_full_snapshot_bytes(),
        rng_state=engine._rng.get_state(),
    )
    branch = chrono.fork(checkpoint_id=checkpoint.checkpoint_id, branch_id="red-team/session-1")

    rebuilt_engine, rebuilt_gateway, _chrono = build_simulation_for_branch(config, branch.branch_id)

    # Same canonical state -> same hash, proving aggregates round-tripped
    # (get_canonical_state_bytes() would differ if owner_id/account_id or
    # any other field get_full_snapshot_bytes() carries were lost).
    assert rebuilt_engine.get_state_hash() == original_hash

    # The rebuilt engine actually has usable, queryable aggregate state —
    # not just a matching hash coincidentally.
    view = rebuilt_engine.get_world_view("user-1", ActorRole.USER)
    assert {a.account_id for a in view.accounts} == {"acc-1"}
    assert view.accounts[0].balance_paise == 10_000

    # commit_strategy is registered on red-team-branch simulations (not on
    # build_simulation()'s plain "main" registry).
    assert rebuilt_gateway._registry.get_tool("commit_strategy") is not None

    # Known limitation (documented in build_simulation_for_branch()'s
    # docstring): the scheduler queue is not restored — pinned here as a
    # regression test so the gap can't silently drift into "actually fixed"
    # without this test being updated too.
    assert rebuilt_engine._env.queue_size == 0


def test_build_simulation_for_branch_rejects_pending_events(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_dag = InMemoryChronoDAG()
    monkeypatch.setattr("sim.main.PostgresChronoDAG", lambda db_url=None: shared_dag)

    config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    engine, _gateway, chrono = build_simulation(config)
    engine.create_account(
        account_id="acc-1", owner_id="user-1",
        account_type=AccountType.PERSONAL, initial_balance_paise=10_000, kyc_level=1,
    )
    checkpoint = chrono.create_checkpoint(
        branch_id="main", event_number=engine._seq_num, sim_time_ns=engine.sim_time_ns,
        state_hash=engine.get_state_hash(), aggregate_snapshot=engine.get_full_snapshot_bytes(),
        rng_state=engine._rng.get_state(),
    )
    branch = chrono.fork(checkpoint_id=checkpoint.checkpoint_id, branch_id="red-team/session-2")

    # First reconstruction succeeds (checkpoint is exactly at branch head).
    branch_engine, _branch_gateway, _chrono = build_simulation_for_branch(config, branch.branch_id)

    # Now the branch's own engine commits a new event past that checkpoint —
    # no fresh checkpoint is taken, so the next checkout() sees a pending
    # event build_simulation_for_branch() can't replay.
    branch_engine.create_account(
        account_id="acc-2", owner_id="user-2",
        account_type=AccountType.PERSONAL, initial_balance_paise=0, kyc_level=1,
    )

    with pytest.raises(NotImplementedError, match="pending_events|event"):
        build_simulation_for_branch(config, branch.branch_id)
