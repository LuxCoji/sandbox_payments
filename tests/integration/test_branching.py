"""Branching Fidelity (Definition-of-Done): forking mid-run with red-agent
activity on the fork must leave the main branch's persisted history
unmutated, and diff() must reflect only the entities the fork touched.

Uses InMemoryChronoDAG (a faithful in-memory ChronoDAG implementation, see
sim/chrono/tests/_fake_dag.py) rather than mocking out psycopg, so this exercises the
real fork/replay/diff algorithms end-to-end without requiring a live
Postgres instance.
"""
from __future__ import annotations

from sim.chrono.tests._fake_dag import InMemoryChronoDAG
from sim.core.engine import WorldEngineImpl
from sim.core.events import AccountCreated
from sim.core.interfaces import AccountType, Command, TransactionType
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG

from ._engine_helpers import aggregate_snapshot_bytes, event_from_stored

NUM_ACCOUNTS = 50
PRE_FORK_TRANSFERS = 500
POST_FORK_RED_AGENT_CALLS = 100


def _seed_accounts(engine: WorldEngineImpl, n: int) -> None:
    for i in range(n):
        engine._apply_event(AccountCreated(
            event_id=f"seed-{i}", event_type="AccountCreated", sim_time_ns=0,
            actor_id="sys", branch_id=engine._branch_id, seq_num=0,
            account_id=f"acc{i}", account_type=AccountType.PERSONAL,
            initial_balance_paise=1_000_000, kyc_level=2, owner_id=f"user{i}",
        ))


def _transfer(engine: WorldEngineImpl, tag: str, i: int, offset: int) -> None:
    src, dst = f"acc{i % NUM_ACCOUNTS}", f"acc{(i + offset) % NUM_ACCOUNTS}"
    engine.execute_command(Command(
        command_id=f"{tag}-{i}", actor_id=src, action_type=TransactionType.TRANSFER,
        source_account_id=src, target_account_id=dst, amount_paise=100,
        idempotency_key=f"{tag}-{i}",
    ))


def test_fork_isolation_and_diff() -> None:
    chrono = InMemoryChronoDAG()
    rng = DeterministicRNG.from_seed(1)
    main_engine = WorldEngineImpl(env=SimulationEnv(), rng=rng, branch_id="main", chrono=chrono)
    _seed_accounts(main_engine, NUM_ACCOUNTS)

    for i in range(PRE_FORK_TRANSFERS):
        _transfer(main_engine, "pre", i, offset=1)

    fork_point = main_engine._seq_num
    assert fork_point == PRE_FORK_TRANSFERS * 2  # AccountDebited + AccountCredited per transfer

    checkpoint = chrono.create_checkpoint(
        branch_id="main", event_number=fork_point, sim_time_ns=main_engine.sim_time_ns,
        state_hash=main_engine.get_state_hash(),
        aggregate_snapshot=aggregate_snapshot_bytes(main_engine), rng_state=rng.get_state(),
    )
    branch = chrono.fork(checkpoint_id=checkpoint.checkpoint_id, branch_id="red-team")
    assert branch.parent_branch_id == "main"
    assert branch.head_seq_num == fork_point

    # Build the forked engine by replaying main's history up to the fork
    # point through the exact same apply_event pipeline (replay invariance).
    red_engine = WorldEngineImpl(
        env=SimulationEnv(), rng=DeterministicRNG.from_seed(1),
        branch_id="red-team", chrono=chrono,
    )
    _seed_accounts(red_engine, NUM_ACCOUNTS)
    for stored in chrono.replay("main", 1, fork_point):
        red_engine._apply_event(event_from_stored(stored))
    red_engine._seq_num = fork_point  # continue the lineage-global seq counter

    assert red_engine.get_state_hash() == main_engine.get_state_hash(), (
        "Replaying main's history onto a fresh engine must reproduce an identical state hash"
    )

    # No divergence yet at the fork point itself
    diff_at_fork = chrono.diff("main", "red-team", fork_point)
    assert diff_at_fork.entities_added == ()
    assert diff_at_fork.entities_removed == ()
    assert diff_at_fork.entities_modified == ()

    # Red-agent activity happens ONLY on the fork
    for i in range(POST_FORK_RED_AGENT_CALLS):
        _transfer(red_engine, "red", i, offset=2)
    red_point = red_engine._seq_num
    assert red_point == fork_point + POST_FORK_RED_AGENT_CALLS * 2

    # Main branch's persisted log must be completely unmutated by fork activity
    main_events_after = chrono.replay("main", 0, 10**9)
    assert len(main_events_after) == PRE_FORK_TRANSFERS * 2
    assert all(e.branch_id == "main" for e in main_events_after)
    assert main_engine.get_state_hash() != red_engine.get_state_hash()
    assert main_engine._seq_num == fork_point  # main's own counter never advanced

    # Diff branches at a shared comparison point: only the accounts the
    # red-agent touched should show up as modified; nothing added/removed.
    diff_tag = red_point
    chrono.create_checkpoint(
        branch_id="main", event_number=diff_tag, sim_time_ns=main_engine.sim_time_ns,
        state_hash=main_engine.get_state_hash(),
        aggregate_snapshot=aggregate_snapshot_bytes(main_engine), rng_state=rng.get_state(),
    )
    chrono.create_checkpoint(
        branch_id="red-team", event_number=diff_tag, sim_time_ns=red_engine.sim_time_ns,
        state_hash=red_engine.get_state_hash(),
        aggregate_snapshot=aggregate_snapshot_bytes(red_engine), rng_state=rng.get_state(),
    )
    diff = chrono.diff("main", "red-team", diff_tag)

    assert diff.entities_added == ()
    assert diff.entities_removed == ()
    touched_ids = {e.entity_id for e in diff.entities_modified}
    assert touched_ids, "red-agent activity must produce at least one modified entity"
    assert touched_ids <= {f"acc{i}" for i in range(NUM_ACCOUNTS)}
    assert diff.events_only_in_a == 0, "main has no events red-team doesn't already share via lineage"
    assert diff.events_only_in_b == POST_FORK_RED_AGENT_CALLS * 2
