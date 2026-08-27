"""Device aggregate."""
from __future__ import annotations

from sim.core.events import DeviceRegistered, DeviceStatusChanged, DomainEvent
from sim.core.interfaces import DeviceSnapshot, DeviceStatus


class Device:
    __slots__ = ("device_id", "owner_id", "device_type", "status", "registered_at_ns")

    def __init__(self, event: DeviceRegistered) -> None:
        self.device_id = event.device_id
        self.owner_id = event.owner_id
        self.device_type = event.device_type
        self.status = DeviceStatus.ACTIVE
        self.registered_at_ns = event.sim_time_ns

    def apply_event(self, event: DomainEvent) -> None:
        if isinstance(event, DeviceStatusChanged):
            self.status = event.new_status

    def to_snapshot(self) -> DeviceSnapshot:
        return DeviceSnapshot(
            device_id=self.device_id,
            owner_id=self.owner_id,
            device_type=self.device_type,
            status=self.status,
            registered_at=self.registered_at_ns,
        )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "device_type": self.device_type.value,
            "status": self.status.value,
        }
