"""Regression test for the demo-checkpoint -> real-engine export chain
(api/main.py::_export_checkpoint_to_postgres, the "Use for Red Team"
bridge) — SimSession.build_engine_from_checkpoint() used to reconstruct
`_processed_idempotency_keys` as a `set`, when WorldEngineImpl treats it as
a `dict[str, CommandResult]` (execute_command() does
`self._processed_idempotency_keys[key] = result`). Pickling that engine's
state (get_full_snapshot_bytes) and restoring it onto a fresh engine
(restore_full_snapshot_bytes) — exactly what the red-team harness does —
then crashed the *next* command on that engine with
`TypeError: 'set' object does not support item assignment`, three hops
away from where the wrong type was actually introduced.
"""
from __future__ import annotations

from api.sim_session import SimSession
from sim.core.engine import WorldEngineImpl
from sim.core.interfaces import Command, TransactionType
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG


def _two_account_ids(session: SimSession) -> tuple[str, str, str]:
    """Returns (funded_account's owner_id, funded_account_id, other_account_id)
    — the command's actor_id has to be the source account's actual owner,
    not an arbitrary string, now that execute_command() enforces that."""
    accounts = list(session.branches["main"].engine._accounts.values())
    funded = next(a for a in accounts if a.balance_paise > 0)
    other = next(a for a in accounts if a.account_id != funded.account_id)
    return funded.owner_id, funded.account_id, other.account_id


def test_checkpoint_export_round_trip_preserves_idempotency_cache_type() -> None:
    session = SimSession(seed=1, num_users=5, num_merchants=1)
    owner_id, src_id, dst_id = _two_account_ids(session)

    checkpoint = session.create_checkpoint("main")
    engine = session.build_engine_from_checkpoint(checkpoint["checkpoint_id"])

    # This is exactly what api/main.py::_export_checkpoint_to_postgres does
    # before handing the bytes to PostgresChronoDAG.import_branch_snapshot().
    snapshot_bytes = engine.get_full_snapshot_bytes()

    fresh_engine = WorldEngineImpl(
        env=SimulationEnv(), rng=DeterministicRNG.from_seed(0), branch_id="red-team/test", chrono=None,
    )
    fresh_engine.restore_full_snapshot_bytes(snapshot_bytes)  # used to succeed silently even with the wrong type

    cmd = Command(
        command_id="c1", actor_id=owner_id, action_type=TransactionType.TRANSFER,
        source_account_id=src_id, target_account_id=dst_id, amount_paise=1, idempotency_key="ik1",
    )
    result = fresh_engine.execute_command(cmd)  # used to raise TypeError here
    assert result.success

    # The idempotency cache itself has to actually work post-restore, not
    # just avoid crashing: a repeat call with the same key must return the
    # cached result rather than debit the account a second time.
    repeat = fresh_engine.execute_command(cmd)
    assert repeat is result
