"""Settlement batch aggregate."""
from __future__ import annotations

from sim.core.events import DomainEvent, SettlementBatchCompleted, SettlementBatchCreated


class SettlementBatch:
    __slots__ = (
        "batch_id",
        "gateway_id",
        "merchant_id",
        "tx_count",
        "total_amount_paise",
        "fee_total_paise",
        "net_amount_paise",
        "status",
    )

    def __init__(self, event: SettlementBatchCreated) -> None:
        self.batch_id = event.batch_id
        self.gateway_id = event.gateway_id
        self.merchant_id = event.merchant_id
        self.tx_count = event.tx_count
        self.total_amount_paise = event.total_amount_paise
        self.fee_total_paise = event.fee_total_paise
        self.net_amount_paise = event.net_amount_paise
        self.status = "PENDING"

    def apply_event(self, event: DomainEvent) -> None:
        if isinstance(event, SettlementBatchCompleted):
            self.status = "COMPLETED"

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "tx_count": self.tx_count,
            "total_amount_paise": self.total_amount_paise,
            "fee_total_paise": self.fee_total_paise,
            "net_amount_paise": self.net_amount_paise,
            "status": self.status,
        }
