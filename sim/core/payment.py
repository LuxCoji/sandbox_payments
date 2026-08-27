"""Payment lifecycle state machine — event-applied aggregate."""
from __future__ import annotations

from sim.core.events import (
    DomainEvent,
    PaymentAuthorized,
    PaymentCaptured,
    PaymentChargedBack,
    PaymentCompleted,
    PaymentDeclined,
    PaymentRefunded,
    PaymentRequested,
    PaymentSettled,
    PaymentTimeout,
    PaymentVoided,
)
from sim.core.interfaces import PaymentStatus, TransactionType

NANOS_PER_SECOND = 1_000_000_000

AUTO_CAPTURE_TYPES: set[TransactionType] = {
    TransactionType.CASH_IN,
    TransactionType.CASH_OUT,
    TransactionType.TRANSFER,
    TransactionType.DEBIT,
}

DEFAULT_TIMEOUT_NS: dict[TransactionType, float] = {
    TransactionType.PAYMENT: 5 * 60 * NANOS_PER_SECOND,      # 5 minutes
    TransactionType.REFUND: 24 * 60 * 60 * NANOS_PER_SECOND,  # 24 hours
}


class Payment:
    __slots__ = (
        "tx_id",
        "tx_type",
        "status",
        "source_account_id",
        "destination_account_id",
        "amount_paise",
        "captured_amount_paise",
        "total_refunded_paise",
        "gateway_id",
        "idempotency_key",
        "created_at_ns",
        "timeout_ns",
    )

    def __init__(self, event: PaymentRequested) -> None:
        self.tx_id = event.tx_id
        self.tx_type = event.tx_type
        self.status = PaymentStatus.INITIATED
        self.source_account_id = event.source_account_id
        self.destination_account_id = event.destination_account_id
        self.amount_paise = event.amount_paise
        self.captured_amount_paise = 0
        self.total_refunded_paise = 0
        self.gateway_id = event.gateway_id
        self.idempotency_key = event.idempotency_key
        self.created_at_ns = event.sim_time_ns
        self.timeout_ns = DEFAULT_TIMEOUT_NS.get(event.tx_type, 0.0)

    def is_auto_capture(self) -> bool:
        return self.tx_type in AUTO_CAPTURE_TYPES

    def can_transition_to(self, new_status: PaymentStatus) -> bool:
        valid_transitions = {
            PaymentStatus.INITIATED: {PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.DECLINED},
            PaymentStatus.AUTHORIZED: {PaymentStatus.CAPTURED, PaymentStatus.VOIDED},
            PaymentStatus.CAPTURED: {PaymentStatus.SETTLED, PaymentStatus.REFUNDED},
            PaymentStatus.SETTLED: {PaymentStatus.COMPLETED},
            PaymentStatus.COMPLETED: {PaymentStatus.CHARGED_BACK},
        }
        # REFUNDED and CHARGED_BACK are pseudo-terminal states
        return new_status in valid_transitions.get(self.status, set())

    def apply_event(self, event: DomainEvent) -> None:
        if isinstance(event, PaymentAuthorized):
            self.status = PaymentStatus.AUTHORIZED
            if event.gateway_id:
                self.gateway_id = event.gateway_id
        elif isinstance(event, PaymentCaptured):
            self.status = PaymentStatus.CAPTURED
            self.captured_amount_paise = self.amount_paise
        elif isinstance(event, PaymentDeclined):
            self.status = PaymentStatus.DECLINED
        elif isinstance(event, PaymentSettled):
            self.status = PaymentStatus.SETTLED
        elif isinstance(event, PaymentCompleted):
            self.status = PaymentStatus.COMPLETED
        elif isinstance(event, PaymentRefunded):
            self.total_refunded_paise += event.refund_amount_paise
            if self.total_refunded_paise >= self.captured_amount_paise:
                self.status = PaymentStatus.REFUNDED
        elif isinstance(event, (PaymentVoided, PaymentTimeout)):
            self.status = PaymentStatus.VOIDED
        elif isinstance(event, PaymentChargedBack):
            self.status = PaymentStatus.CHARGED_BACK

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "tx_type": self.tx_type.value,
            "status": self.status.value,
            "amount_paise": self.amount_paise,
            "captured_amount_paise": self.captured_amount_paise,
            "total_refunded_paise": self.total_refunded_paise,
        }
