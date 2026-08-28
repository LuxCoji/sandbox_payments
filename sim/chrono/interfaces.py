"""Chrono subsystem contracts.

Defines the ChronoDAG protocol for branch-aware event sourcing,
checkpointing, replay, and state diffing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class StoredEvent:
    """Envelope for an event persisted in the ChronoDAG store.

    This wraps the domain event payload with branch-aware metadata.
    """
    event_id: str                            # UUIDv7
    event_type: str                          # Discriminator (e.g., "AccountCreated")
    sim_time_ns: float
    actor_id: str | None
    branch_id: str                           # Branch this event belongs to
    seq_num: int                             # Monotonic within branch
    payload: dict[str, object]               # Serialized domain event data
    causation_id: str | None = None          # ID of triggering event
    correlation_id: str | None = None        # Groups multi-step flows


@dataclass(frozen=True)
class Checkpoint:
    """Snapshot of simulation state at a specific event."""
    checkpoint_id: str                       # UUIDv7
    branch_id: str
    event_number: int                        # seq_num at which snapshot was taken
    sim_time_ns: float
    state_hash: str                          # SHA-256 of canonical state
    aggregate_snapshot: bytes                # Serialized aggregate state
    rng_state: bytes                         # DeterministicRNG state for restoration
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Branch:
    """A branch in the ChronoDAG, tracking lineage and metadata."""
    branch_id: str                           # UUIDv7
    parent_checkpoint_id: str | None         # None for root branch ("main")
    parent_branch_id: str | None             # None for root branch
    created_at_ns: float                     # Sim time when branch was forked
    seed_offset: int                         # Derived seed offset for branch RNG
    head_seq_num: int                        # Current head event sequence number
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayContext:
    """Context returned when checking out a branch for replay."""
    branch: Branch
    checkpoint: Checkpoint
    pending_events: tuple[StoredEvent, ...]  # Events after checkpoint to replay


@dataclass(frozen=True)
class FieldDelta:
    """A single field-level change between two states."""
    field_name: str
    old_value: object
    new_value: object


@dataclass(frozen=True)
class EntityDiff:
    """Changes to a single entity between two branches."""
    entity_type: str                         # "account", "device", "merchant", etc.
    entity_id: str
    changes: tuple[FieldDelta, ...]


@dataclass(frozen=True)
class StateDiff:
    """Recursive delta between states of two branches at a given event."""
    branch_a_id: str
    branch_b_id: str
    at_event: int
    entities_added: tuple[EntityDiff, ...]   # Present in B, absent in A
    entities_removed: tuple[EntityDiff, ...]  # Present in A, absent in B
    entities_modified: tuple[EntityDiff, ...]  # Changed between A and B
    events_only_in_a: int                    # Count of events unique to branch A
    events_only_in_b: int                    # Count of events unique to branch B


@runtime_checkable
class ChronoDAG(Protocol):
    """Protocol for the branch-aware event store.

    The ChronoDAG is the persistence backbone of the simulation.
    It stores all domain events in branch-aware lineages, supports
    forking, checkpointing, replay, and state diffing.
    """

    def save_event(self, event: StoredEvent) -> None:
        """Append an event to the current branch log."""
        ...

    def save_events(self, events: list[StoredEvent]) -> None:
        """Append multiple events in a single transaction."""
        ...

    def create_checkpoint(
        self,
        branch_id: str,
        event_number: int,
        sim_time_ns: float,
        state_hash: str,
        aggregate_snapshot: bytes,
        rng_state: bytes,
        metadata: dict[str, object] | None = None,
    ) -> Checkpoint:
        """Capture state snapshot at the given event number.

        Includes:
            - Canonical state hash (SHA-256)
            - Serialized aggregate state
            - RNG state bytes for deterministic restoration
        """
        ...

    def fork(
        self,
        checkpoint_id: str,
        branch_id: str,
        metadata: dict[str, object] | None = None,
    ) -> Branch:
        """Create a new branch from a checkpoint.

        Records branch lineage with a derived seed offset
        for independent RNG streams on the fork.
        """
        ...

    def checkout(self, branch_id: str) -> ReplayContext:
        """Restore state from the latest checkpoint on a branch.

        Returns a ReplayContext containing:
            - The nearest checkpoint
            - Events after the checkpoint to be replayed
        """
        ...

    def diff(self, branch_a: str, branch_b: str, at_event: int) -> StateDiff:
        """Diff two branches exactly at a given sequence number."""
        ...

    def delete_branch(self, branch_id: str) -> None:
        """Deletes a branch and all its associated events and checkpoints.
        Raises ValueError if attempting to delete 'main' or a branch that has children.
        """
        ...

    def reset(self) -> None:
        """Clears all data from the DAG, resetting it to its initial state."""
        ...

    def replay(
        self, branch_id: str, from_event: int, to_event: int
    ) -> list[StoredEvent]:
        """Retrieve a range of events from a branch log."""
        ...

    def get_state_hash(self, branch_id: str, event_number: int) -> str:
        """Return SHA-256 state digest at a specific event on a branch."""
        ...
