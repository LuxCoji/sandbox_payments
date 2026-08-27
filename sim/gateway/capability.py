"""Capability checking utilities."""
from __future__ import annotations

from sim.gateway.interfaces import ROLE_CAPABILITIES, ActorContext, ActorRole, Capability


def get_capabilities_for_role(role: ActorRole) -> frozenset[Capability]:
    return ROLE_CAPABILITIES.get(role, frozenset())


def has_capability(context: ActorContext, capability: Capability) -> bool:
    return capability in context.capabilities
