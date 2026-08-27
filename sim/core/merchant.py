"""Merchant aggregate."""
from __future__ import annotations

from sim.core.events import DomainEvent, MerchantOnboarded, MerchantSuspended
from sim.core.interfaces import MerchantDirectoryEntry


class Merchant:
    __slots__ = (
        "merchant_id",
        "name",
        "category",
        "settlement_rail",
        "status",
        "avg_rating",
    )

    def __init__(self, event: MerchantOnboarded) -> None:
        self.merchant_id = event.merchant_id
        self.name = event.name
        self.category = event.category
        self.settlement_rail = event.settlement_rail
        self.status = "ACTIVE"
        self.avg_rating = 0.0

    def apply_event(self, event: DomainEvent) -> None:
        if isinstance(event, MerchantSuspended):
            self.status = "SUSPENDED"

    def to_directory_entry(self) -> MerchantDirectoryEntry:
        return MerchantDirectoryEntry(
            merchant_id=self.merchant_id,
            name=self.name,
            category=self.category,
            avg_rating=self.avg_rating,
            settlement_rail=self.settlement_rail,
        )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "settlement_rail": self.settlement_rail,
            "status": self.status,
            "avg_rating": self.avg_rating,
        }
