"""Unit tests for WorldEngine."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sim.core.engine import WorldEngineImpl
from sim.core.events import AccountCreated
from sim.core.interfaces import AccountType, ActorRole, Command, TransactionType
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG

if TYPE_CHECKING:
    from sim.chrono.interfaces import StoredEvent


class FakeChronoDAG:
    """Minimal in-memory ChronoDAG stand-in that only records save_event calls."""

    def __init__(self) -> None:
        self.saved: list[StoredEvent] = []

    def save_event(self, event: StoredEvent) -> None:
        self.saved.append(event)


def test_create_account_and_view(engine: WorldEngineImpl) -> None:
    # We apply a creation event directly to simulate population or setup
    event = AccountCreated(
        event_id="e1", event_type="AccountCreated", sim_time_ns=0, actor_id="sys",
        branch_id="main", seq_num=1, account_id="acc1", account_type=AccountType.PERSONAL,
        initial_balance_paise=1000, kyc_level=1, owner_id="user1"
    )
    # Using internal _apply_event for setup
    engine._apply_event(event)

    view = engine.get_world_view("user1", ActorRole.USER)
    assert len(view.accounts) == 1
    assert view.accounts[0].account_id == "acc1"
    assert view.accounts[0].balance_paise == 1000

def test_transfer_success(engine: WorldEngineImpl) -> None:
    e1 = AccountCreated(event_id="e1", event_type="AccountCreated", sim_time_ns=0, actor_id="sys", branch_id="main", seq_num=1, account_id="acc1", account_type=AccountType.PERSONAL, initial_balance_paise=1000, kyc_level=1, owner_id="user1")
    e2 = AccountCreated(event_id="e2", event_type="AccountCreated", sim_time_ns=0, actor_id="sys", branch_id="main", seq_num=2, account_id="acc2", account_type=AccountType.PERSONAL, initial_balance_paise=0, kyc_level=1, owner_id="user2")
    engine._apply_event(e1)
    engine._apply_event(e2)

    cmd = Command(
        command_id="c1", actor_id="user1", action_type=TransactionType.TRANSFER,
        source_account_id="acc1", target_account_id="acc2", amount_paise=500, idempotency_key="ik1"
    )
    result = engine.execute_command(cmd)

    assert result.success
    assert len(result.events) == 2
    assert result.events[0].event_type == "AccountDebited"
    assert result.events[1].event_type == "AccountCredited"

    view1 = engine.get_world_view("user1", ActorRole.USER)
    assert view1.accounts[0].balance_paise == 500

    view2 = engine.get_world_view("user2", ActorRole.USER)
    assert view2.accounts[0].balance_paise == 500


def _seed_account(engine: WorldEngineImpl, account_id: str, owner_id: str, balance: int) -> None:
    engine._apply_event(AccountCreated(
        event_id=f"seed-{account_id}", event_type="AccountCreated", sim_time_ns=0,
        actor_id="sys", branch_id="main", seq_num=0, account_id=account_id,
        account_type=AccountType.PERSONAL, initial_balance_paise=balance, kyc_level=1,
        owner_id=owner_id,
    ))


def test_execute_command_persists_events_to_chrono() -> None:
    """Phase 1: execute_command must append every emitted event to ChronoDAG."""
    chrono = FakeChronoDAG()
    engine = WorldEngineImpl(env=SimulationEnv(), rng=DeterministicRNG.from_seed(1), chrono=chrono)
    _seed_account(engine, "acc1", "user1", 1000)
    _seed_account(engine, "acc2", "user2", 0)

    cmd = Command(
        command_id="c1", actor_id="user1", action_type=TransactionType.TRANSFER,
        source_account_id="acc1", target_account_id="acc2", amount_paise=500, idempotency_key="ik1"
    )
    result = engine.execute_command(cmd)

    assert len(chrono.saved) == len(result.events) == 2
    assert [e.event_type for e in chrono.saved] == ["AccountDebited", "AccountCredited"]
    # payload carries the domain fields, not the envelope fields (those are top-level on StoredEvent)
    assert chrono.saved[0].payload["account_id"] == "acc1"
    assert chrono.saved[0].payload["amount_paise"] == 500
    assert "event_id" not in chrono.saved[0].payload


def test_execute_command_without_chrono_is_a_noop() -> None:
    """No ChronoDAG wired (e.g. isolated unit tests) must not raise."""
    engine = WorldEngineImpl(env=SimulationEnv(), rng=DeterministicRNG.from_seed(1))
    _seed_account(engine, "acc1", "user1", 1000)
    _seed_account(engine, "acc2", "user2", 0)

    cmd = Command(
        command_id="c1", actor_id="user1", action_type=TransactionType.TRANSFER,
        source_account_id="acc1", target_account_id="acc2", amount_paise=500, idempotency_key="ik1"
    )
    result = engine.execute_command(cmd)
    assert result.success


def test_pii_masking_hides_owner_id_for_bank_ops(engine: WorldEngineImpl) -> None:
    _seed_account(engine, "acc1", "user1", 1000)

    own_view = engine.get_world_view("user1", ActorRole.USER)
    assert own_view.accounts[0].owner_id == "user1"

    masked_view = engine.get_world_view("ops1", ActorRole.BANK_OPS)
    assert masked_view.accounts[0].owner_id != "user1"
    assert masked_view.accounts[0].owner_id != ""


def test_chargeback_reverses_funds(engine: WorldEngineImpl) -> None:
    _seed_account(engine, "acc1", "merchant1", 1000)
    _seed_account(engine, "acc2", "user1", 0)

    cmd = Command(
        command_id="c1", actor_id="sys", action_type=TransactionType.CHARGEBACK,
        source_account_id="acc1", target_account_id="acc2", amount_paise=300, idempotency_key="ik1"
    )
    result = engine.execute_command(cmd)

    assert result.success
    assert [type(e).__name__ for e in result.events] == [
        "AccountDebited", "AccountCredited", "PaymentChargedBack"
    ]
    assert engine.get_world_view("merchant1", ActorRole.USER).accounts[0].balance_paise == 700
    assert engine.get_world_view("user1", ActorRole.USER).accounts[0].balance_paise == 300


def test_settlement_credits_target(engine: WorldEngineImpl) -> None:
    _seed_account(engine, "acc1", "merchant1", 0)

    cmd = Command(
        command_id="c1", actor_id="sys", action_type=TransactionType.SETTLEMENT,
        source_account_id=None, target_account_id="acc1", amount_paise=1000, idempotency_key="ik1",
        metadata={"fee_paise": 50},
    )
    result = engine.execute_command(cmd)

    assert result.success
    event_types = [type(e).__name__ for e in result.events]
    assert "SettlementBatchCreated" in event_types
    assert "SettlementBatchCompleted" in event_types
    assert engine.get_world_view("merchant1", ActorRole.USER).accounts[0].balance_paise == 950


def test_fee_debits_account(engine: WorldEngineImpl) -> None:
    _seed_account(engine, "acc1", "user1", 1000)

    cmd = Command(
        command_id="c1", actor_id="sys", action_type=TransactionType.FEE,
        source_account_id="acc1", target_account_id=None, amount_paise=100, idempotency_key="ik1"
    )
    result = engine.execute_command(cmd)

    assert result.success
    assert [type(e).__name__ for e in result.events] == ["AccountDebited", "FeeCharged"]
    assert engine.get_world_view("user1", ActorRole.USER).accounts[0].balance_paise == 900


def test_execute_command_increments_events_processed_metric(engine: WorldEngineImpl) -> None:
    """Phase 3: every applied event must increment the events_processed counter."""
    from sim.observability import EVENTS_PROCESSED

    _seed_account(engine, "acc1", "user1", 1000)
    _seed_account(engine, "acc2", "user2", 0)
    before = EVENTS_PROCESSED.labels(event_type="AccountDebited", branch_id="main")._value.get()

    cmd = Command(
        command_id="c1", actor_id="user1", action_type=TransactionType.TRANSFER,
        source_account_id="acc1", target_account_id="acc2", amount_paise=500, idempotency_key="ik1"
    )
    engine.execute_command(cmd)

    after = EVENTS_PROCESSED.labels(event_type="AccountDebited", branch_id="main")._value.get()
    assert after == before + 1


def test_interest_credits_account(engine: WorldEngineImpl) -> None:
    _seed_account(engine, "acc1", "user1", 1000)

    cmd = Command(
        command_id="c1", actor_id="sys", action_type=TransactionType.INTEREST,
        source_account_id=None, target_account_id="acc1", amount_paise=25, idempotency_key="ik1"
    )
    result = engine.execute_command(cmd)

    assert result.success
    assert [type(e).__name__ for e in result.events] == ["AccountCredited", "InterestAccrued"]
    assert engine.get_world_view("user1", ActorRole.USER).accounts[0].balance_paise == 1025

def test_transfer_insufficient_funds(engine: WorldEngineImpl) -> None:
    e1 = AccountCreated(event_id="e1", event_type="AccountCreated", sim_time_ns=0, actor_id="sys", branch_id="main", seq_num=1, account_id="acc1", account_type=AccountType.PERSONAL, initial_balance_paise=100, kyc_level=1, owner_id="user1")
    e2 = AccountCreated(event_id="e2", event_type="AccountCreated", sim_time_ns=0, actor_id="sys", branch_id="main", seq_num=2, account_id="acc2", account_type=AccountType.PERSONAL, initial_balance_paise=0, kyc_level=1, owner_id="user2")
    engine._apply_event(e1)
    engine._apply_event(e2)

    cmd = Command(
        command_id="c1", actor_id="user1", action_type=TransactionType.TRANSFER,
        source_account_id="acc1", target_account_id="acc2", amount_paise=500, idempotency_key="ik1"
    )
    result = engine.execute_command(cmd)

    assert not result.success
    assert len(result.events) == 1
    assert result.events[0].event_type == "TransferRejected"
    assert "Insufficient funds" in result.events[0].detail
