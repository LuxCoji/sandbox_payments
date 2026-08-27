"""Gateway aggregate (not the gateway subsystem)."""
from __future__ import annotations

from sim.core.events import DomainEvent, GatewayStatusChanged


class GatewayEntity:
    __slots__ = ("gateway_id", "status", "supported_types", "fee_schedule_id")

    def __init__(self, gateway_id: str) -> None:
        self.gateway_id = gateway_id
        self.status = "ACTIVE"
        self.supported_types: set[str] = set()
        self.fee_schedule_id: str | None = None

    def apply_event(self, event: DomainEvent) -> None:
        if isinstance(event, GatewayStatusChanged):
            self.status = event.new_status

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "supported_types": sorted(self.supported_types),
        }
