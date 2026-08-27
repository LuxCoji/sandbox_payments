"""Unit tests for WorldEngine."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sim.core.events import AccountCreated
from sim.core.interfaces import AccountType, ActorRole, Command, TransactionType

if TYPE_CHECKING:
    from sim.core.engine import WorldEngineImpl


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
