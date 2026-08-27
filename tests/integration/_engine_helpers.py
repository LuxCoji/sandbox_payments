"""Shared helpers for building ChronoDAG checkpoints/replay from a WorldEngine."""
from __future__ import annotations

import enum
import json
import types as builtin_types
import typing
from typing import TYPE_CHECKING

from sim.core import events as events_module

if TYPE_CHECKING:
    from sim.chrono.interfaces import StoredEvent
    from sim.core.engine import WorldEngineImpl
    from sim.core.events import DomainEvent


def aggregate_snapshot_bytes(engine: WorldEngineImpl) -> bytes:
    """Canonical JSON snapshot of the engine's aggregates, keyed the way
    PostgresChronoDAG.diff() expects: dict[entity_type][entity_id] -> fields."""
    snapshot = {
        "accounts": {k: v.to_canonical_dict() for k, v in engine._accounts.items()},
        "payments": {k: v.to_canonical_dict() for k, v in engine._payments.items()},
        "devices": {k: v.to_canonical_dict() for k, v in engine._devices.items()},
        "merchants": {k: v.to_canonical_dict() for k, v in engine._merchants.items()},
    }
    return json.dumps(snapshot, sort_keys=True).encode()


def _enum_type(hint: object) -> type[enum.Enum] | None:
    """If `hint` is an Enum subclass (optionally wrapped in `X | None`), return it."""
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return hint
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is builtin_types.UnionType:
        for arg in typing.get_args(hint):
            if isinstance(arg, type) and issubclass(arg, enum.Enum):
                return arg
    return None


def event_from_stored(stored: StoredEvent) -> DomainEvent:
    """Reconstruct the concrete DomainEvent subclass from a StoredEvent envelope +
    payload, converting JSON-ified enum values (plain strings) back to their
    proper Enum members based on the event class's field type hints."""
    event_cls = getattr(events_module, stored.event_type)
    hints = typing.get_type_hints(event_cls)

    kwargs = dict(stored.payload)
    for key, value in list(kwargs.items()):
        enum_cls = _enum_type(hints.get(key))
        if enum_cls is not None and not isinstance(value, enum.Enum):
            kwargs[key] = enum_cls(value)

    return typing.cast("DomainEvent", event_cls(
        event_id=stored.event_id,
        event_type=stored.event_type,
        sim_time_ns=stored.sim_time_ns,
        actor_id=stored.actor_id,
        branch_id=stored.branch_id,
        seq_num=stored.seq_num,
        causation_id=stored.causation_id,
        correlation_id=stored.correlation_id,
        **kwargs,
    ))
