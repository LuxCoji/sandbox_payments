"""Payment lifecycle tests."""
from __future__ import annotations

from sim.core.events import (
    PaymentAuthorized,
    PaymentCaptured,
    PaymentRequested,
)
from sim.core.interfaces import PaymentStatus, TransactionType
from sim.core.payment import Payment


def test_payment_lifecycle() -> None:
    # 1. Initiated
    req = PaymentRequested(
        event_id="e1", event_type="PaymentRequested", sim_time_ns=0, actor_id="user1",
        branch_id="main", seq_num=1, tx_id="tx1", tx_type=TransactionType.PAYMENT,
        source_account_id="acc1", destination_account_id="acc2", amount_paise=1000
    )
    payment = Payment(req)
    assert payment.status == PaymentStatus.INITIATED
    assert payment.amount_paise == 1000

    # 2. Authorized
    auth = PaymentAuthorized(
        event_id="e2", event_type="PaymentAuthorized", sim_time_ns=0, actor_id="gw1",
        branch_id="main", seq_num=2, tx_id="tx1", gateway_id="gw1"
    )
    assert payment.can_transition_to(PaymentStatus.AUTHORIZED)
    payment.apply_event(auth)
    assert payment.status == PaymentStatus.AUTHORIZED
    assert payment.gateway_id == "gw1"

    # 3. Captured
    cap = PaymentCaptured(
        event_id="e3", event_type="PaymentCaptured", sim_time_ns=0, actor_id="gw1",
        branch_id="main", seq_num=3, tx_id="tx1"
    )
    assert payment.can_transition_to(PaymentStatus.CAPTURED)
    payment.apply_event(cap)
    assert payment.status == PaymentStatus.CAPTURED
    assert payment.captured_amount_paise == 1000
