# ChronoDAG Design

## Overview

`ChronoDAG` is the branch-aware event store protocol defined in `sim/chrono/interfaces.py`. It provides the storage, branching, checkpointing, and replay foundation for the simulation.

*Note: Per `initial_plan.md`, implementation of the ChronoDAG store is owned by Person 3.*

---

## Core Interfaces & Types

### 1. Persistence Envelope (`StoredEvent`)
Wraps domain events with branch-aware metadata:
- `event_id: str` (UUIDv7)
- `event_type: str`
- `sim_time_ns: float`
- `actor_id: str | None`
- `branch_id: str`
- `seq_num: int`
- `payload: dict[str, object]`
- `causation_id: str | None`
- `correlation_id: str | None`

### 2. Checkpoints (`Checkpoint`)
Captures full snapshot at a specific event:
- `checkpoint_id: str`
- `branch_id: str`
- `event_number: int`
- `sim_time_ns: float`
- `state_hash: str` (SHA-256)
- `aggregate_snapshot: bytes`
- `rng_state: bytes` (from `DeterministicRNG.get_state()`)

### 3. Branching (`Branch`)
- `branch_id: str`
- `parent_checkpoint_id: str | None`
- `parent_branch_id: str | None`
- `created_at_ns: float`
- `seed_offset: int`
- `head_seq_num: int`

### 4. State Diffing (`StateDiff`)
- `branch_a_id: str`
- `branch_b_id: str`
- `at_event: int`
- `entities_added: tuple[EntityDiff, ...]`
- `entities_removed: tuple[EntityDiff, ...]`
- `entities_modified: tuple[EntityDiff, ...]`
- `events_only_in_a: int`
- `events_only_in_b: int`

---

## Protocol Definition (`ChronoDAG`)

Defined in `sim/chrono/interfaces.py`:

```python
class ChronoDAG(Protocol):
    def save_event(self, event: StoredEvent) -> None: ...
    def create_checkpoint(
        self, branch_id: str, event_number: int, sim_time_ns: float, state_hash: str,
        aggregate_snapshot: bytes, rng_state: bytes, metadata: dict[str, object] | None = None,
    ) -> Checkpoint: ...
    def fork(self, checkpoint_id: str, branch_id: str, metadata: dict[str, object] | None = None) -> Branch: ...
    def checkout(self, branch_id: str) -> ReplayContext: ...
    def diff(self, branch_a: str, branch_b: str, at_event: int) -> StateDiff: ...
    def replay(self, branch_id: str, from_event: int, to_event: int) -> list[StoredEvent]: ...
    def get_state_hash(self, branch_id: str, event_number: int) -> str: ...
```
