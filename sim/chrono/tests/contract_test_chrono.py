"""Contract tests for the ChronoDAG protocol.

Exercises real fork/checkout/diff/replay/get_state_hash behavior against
InMemoryChronoDAG (a faithful in-memory ChronoDAG implementation — same
lineage/diff algorithms as PostgresChronoDAG, dict-backed instead of
SQL-backed) rather than just checking method existence, so these actually
verify protocol conformance and not just attribute presence.
"""
from __future__ import annotations

import pytest

from sim.chrono.interfaces import ChronoDAG, FieldDelta, StoredEvent
from sim.chrono.tests._fake_dag import InMemoryChronoDAG


def _event(branch_id: str, seq_num: int, event_type: str = "AccountDebited") -> StoredEvent:
    return StoredEvent(
        event_id=f"{branch_id}-{seq_num}", event_type=event_type, sim_time_ns=float(seq_num),
        actor_id="user1", branch_id=branch_id, seq_num=seq_num,
        payload={"account_id": "acc1", "amount_paise": 100},
    )


def test_chrono_dag_satisfies_protocol() -> None:
    assert isinstance(InMemoryChronoDAG(), ChronoDAG)


def test_save_and_replay_round_trips_events() -> None:
    dag = InMemoryChronoDAG()
    for i in range(1, 6):
        dag.save_event(_event("main", i))

    events = dag.replay("main", 1, 5)
    assert [e.seq_num for e in events] == [1, 2, 3, 4, 5]
    assert dag._branches["main"].head_seq_num == 5


def test_fork_produces_independent_seed_offset_and_lineage() -> None:
    dag = InMemoryChronoDAG()
    for i in range(1, 4):
        dag.save_event(_event("main", i))
    cp = dag.create_checkpoint(
        branch_id="main", event_number=3, sim_time_ns=3.0,
        state_hash="h1", aggregate_snapshot=b'{"accounts": {}}', rng_state=b"rng",
    )

    branch_a = dag.fork(checkpoint_id=cp.checkpoint_id, branch_id="branch-a")
    branch_b = dag.fork(checkpoint_id=cp.checkpoint_id, branch_id="branch-b")

    assert branch_a.parent_branch_id == "main"
    assert branch_a.parent_checkpoint_id == cp.checkpoint_id
    assert branch_a.head_seq_num == 3
    # Independent RNG stream derivation per fork (per rng_design.md: seed_offset
    # is derived from the branch name, so different branch names diverge).
    assert branch_a.seed_offset != branch_b.seed_offset


def test_update_branch_metadata_overwrites_and_returns_branch() -> None:
    dag = InMemoryChronoDAG()
    for i in range(1, 4):
        dag.save_event(_event("main", i))
    cp = dag.create_checkpoint(
        branch_id="main", event_number=3, sim_time_ns=3.0,
        state_hash="h1", aggregate_snapshot=b'{"accounts": {}}', rng_state=b"rng",
    )
    branch = dag.fork(checkpoint_id=cp.checkpoint_id, branch_id="red-team/session-1/attempt-a",
                       metadata={"origin": "agent_experiment"})
    assert branch.metadata == {"origin": "agent_experiment"}

    updated = dag.update_branch_metadata(
        branch.branch_id, {**branch.metadata, "origin": "committed"}
    )
    assert updated.metadata == {"origin": "committed"}
    # Other Branch fields (lineage, seed_offset, head_seq_num) unchanged.
    assert updated.parent_branch_id == branch.parent_branch_id
    assert updated.seed_offset == branch.seed_offset
    assert updated.head_seq_num == branch.head_seq_num


def test_update_branch_metadata_unknown_branch_raises() -> None:
    dag = InMemoryChronoDAG()
    with pytest.raises(ValueError, match="not found"):
        dag.update_branch_metadata("does-not-exist", {"origin": "committed"})


def test_replay_after_fork_inherits_parent_lineage_then_diverges() -> None:
    dag = InMemoryChronoDAG()
    for i in range(1, 4):
        dag.save_event(_event("main", i))
    cp = dag.create_checkpoint(
        branch_id="main", event_number=3, sim_time_ns=3.0,
        state_hash="h1", aggregate_snapshot=b'{"accounts": {}}', rng_state=b"rng",
    )
    dag.fork(checkpoint_id=cp.checkpoint_id, branch_id="fork")
    dag.save_event(_event("fork", 4))
    dag.save_event(_event("fork", 5))

    # Replay on the fork covers BOTH the inherited main events and its own new ones.
    replayed = dag.replay("fork", 0, 10)
    assert [e.seq_num for e in replayed] == [1, 2, 3, 4, 5]
    assert [e.branch_id for e in replayed] == ["main", "main", "main", "fork", "fork"]

    # Main's own lineage/log is untouched by the fork's new events.
    main_only = dag.replay("main", 0, 10)
    assert [e.seq_num for e in main_only] == [1, 2, 3]


def test_checkout_returns_latest_checkpoint_and_pending_events() -> None:
    dag = InMemoryChronoDAG()
    for i in range(1, 4):
        dag.save_event(_event("main", i))
    cp = dag.create_checkpoint(
        branch_id="main", event_number=3, sim_time_ns=3.0,
        state_hash="h1", aggregate_snapshot=b'{"accounts": {}}', rng_state=b"rng",
    )
    dag.save_event(_event("main", 4))
    dag.save_event(_event("main", 5))

    ctx = dag.checkout("main")
    assert ctx.checkpoint.checkpoint_id == cp.checkpoint_id
    assert [e.seq_num for e in ctx.pending_events] == [4, 5]


def test_diff_reports_no_changes_for_identical_snapshots() -> None:
    dag = InMemoryChronoDAG()
    dag.save_event(_event("main", 1))
    snapshot = b'{"accounts": {"acc1": {"balance_paise": 100}}}'
    cp = dag.create_checkpoint(
        branch_id="main", event_number=1, sim_time_ns=1.0,
        state_hash="h", aggregate_snapshot=snapshot, rng_state=b"",
    )
    dag.fork(checkpoint_id=cp.checkpoint_id, branch_id="branch-a")
    dag.create_checkpoint(
        branch_id="branch-a", event_number=1, sim_time_ns=1.0,
        state_hash="h", aggregate_snapshot=snapshot, rng_state=b"",
    )

    diff = dag.diff("main", "branch-a", 1)
    assert diff.entities_added == ()
    assert diff.entities_removed == ()
    assert diff.entities_modified == ()
    assert diff.events_only_in_a == 0
    assert diff.events_only_in_b == 0


def test_diff_reports_modified_and_added_entities() -> None:
    dag = InMemoryChronoDAG()
    dag.save_event(_event("main", 1))
    cp = dag.create_checkpoint(
        branch_id="main", event_number=1, sim_time_ns=1.0, state_hash="h1",
        aggregate_snapshot=b'{"accounts": {"acc1": {"balance_paise": 100}}}', rng_state=b"",
    )
    dag.fork(checkpoint_id=cp.checkpoint_id, branch_id="branch-a")
    dag.create_checkpoint(
        branch_id="branch-a", event_number=1, sim_time_ns=1.0, state_hash="h2",
        aggregate_snapshot=b'{"accounts": {"acc1": {"balance_paise": 200}, "acc2": {"balance_paise": 50}}}',
        rng_state=b"",
    )

    diff = dag.diff("main", "branch-a", 1)
    assert {e.entity_id for e in diff.entities_added} == {"acc2"}
    assert diff.entities_removed == ()
    modified = {e.entity_id: e.changes for e in diff.entities_modified}
    assert set(modified) == {"acc1"}
    assert modified["acc1"] == (FieldDelta("balance_paise", 100, 200),)


def test_get_state_hash_returns_checkpoint_hash() -> None:
    dag = InMemoryChronoDAG()
    for i in range(1, 6):
        dag.save_event(_event("main", i))
    dag.create_checkpoint(
        branch_id="main", event_number=5, sim_time_ns=5.0,
        state_hash="deadbeef", aggregate_snapshot=b"{}", rng_state=b"",
    )
    assert dag.get_state_hash("main", 5) == "deadbeef"
