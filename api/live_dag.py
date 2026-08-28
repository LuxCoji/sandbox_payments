"""In-process, broadcast-capable ChronoDAG for the demo API/frontend.

This is a standalone implementation of the `sim.chrono.interfaces.ChronoDAG`
protocol (duck-typed — Protocol is `@runtime_checkable`), living outside the
`sim` package on purpose: it is a *consumer* of `sim`, not a subsystem, so it
is not bound by the cross-subsystem import-linter contracts, but it still
only imports `sim.chrono.interfaces` / `sim.core.interfaces` dataclasses —
never a concrete `sim.*` module — to stay honest about that boundary.

Differences from `PostgresChronoDAG` (which needs `FINSIM_DB_URL`) and the
test-only `InMemoryChronoDAG` fake:
  - Implements the same fork/checkpoint/diff/replay algorithms (branch
    lineage resolution, per-branch seq_num continuation from the fork
    point) so branching semantics shown in the UI are real, not mocked.
  - Adds `list_branches()` / `list_events()` (not part of the ChronoDAG
    protocol — the real store has no "browse" API, only replay-by-range)
    since a UI needs to enumerate branches/events, not just replay them.
  - Adds a pub/sub hook (`subscribe()`) so `save_event()` fans new events
    out to connected WebSocket clients in real time.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

from sim.chrono.interfaces import (
    Branch,
    Checkpoint,
    EntityDiff,
    FieldDelta,
    ReplayContext,
    StateDiff,
    StoredEvent,
)


@dataclass
class _BranchRow:
    branch: Branch
    events: list[StoredEvent] = field(default_factory=list)
    checkpoints: dict[int, Checkpoint] = field(default_factory=dict)
    name: str = "main"
    created_wall_ns: float = field(default_factory=lambda: time.time() * 1e9)


class LiveChronoDAG:
    """Process-wide, broadcast-capable ChronoDAG. Not thread-safe by design —
    intended to be driven from a single asyncio event loop."""

    def __init__(self) -> None:
        self._branches: dict[str, _BranchRow] = {}
        self._checkpoints_by_id: dict[str, Checkpoint] = {}
        self._subscribers: set[asyncio.Queue] = set()

        root = Branch(
            branch_id="main",
            parent_checkpoint_id=None,
            parent_branch_id=None,
            created_at_ns=0.0,
            seed_offset=0,
            head_seq_num=0,
        )
        self._branches["main"] = _BranchRow(branch=root, checkpoints={}, name="main")

    # ── pub/sub ─────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, event: StoredEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ── ChronoDAG protocol ─────────────────────────────────────────────

    def save_event(self, event: StoredEvent) -> None:
        self.save_events([event])

    def save_events(self, events: list[StoredEvent]) -> None:
        if not events:
            return

        last_event = max(events, key=lambda e: e.seq_num)
        row = self._branches[last_event.branch_id]

        for event in events:
            row.events.append(event)
            self._broadcast(event)

        row.branch = Branch(
            branch_id=row.branch.branch_id,
            parent_checkpoint_id=row.branch.parent_checkpoint_id,
            parent_branch_id=row.branch.parent_branch_id,
            created_at_ns=row.branch.created_at_ns,
            seed_offset=row.branch.seed_offset,
            head_seq_num=max(row.branch.head_seq_num, last_event.seq_num),
            metadata=row.branch.metadata,
        )

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
        cp = Checkpoint(
            checkpoint_id=f"cp-{branch_id}-{event_number}-{len(self._checkpoints_by_id)}",
            branch_id=branch_id,
            event_number=event_number,
            sim_time_ns=sim_time_ns,
            state_hash=state_hash,
            aggregate_snapshot=aggregate_snapshot,
            rng_state=rng_state,
            metadata=metadata or {},
        )
        self._branches[branch_id].checkpoints[event_number] = cp
        self._checkpoints_by_id[cp.checkpoint_id] = cp
        return cp

    def fork(
        self, checkpoint_id: str, branch_id: str, metadata: dict[str, object] | None = None
    ) -> Branch:
        cp = self._checkpoints_by_id[checkpoint_id]
        parent_row = self._branches[cp.branch_id]
        branch = Branch(
            branch_id=branch_id,
            parent_checkpoint_id=checkpoint_id,
            parent_branch_id=cp.branch_id,
            created_at_ns=cp.sim_time_ns,
            seed_offset=len(self._branches),
            head_seq_num=cp.event_number,
            metadata=metadata or {},
        )
        self._branches[branch_id] = _BranchRow(
            branch=branch, checkpoints={}, name=str((metadata or {}).get("name", branch_id))
        )
        return branch

    def delete_branch(self, branch_id: str) -> None:
        if branch_id == "main":
            raise ValueError("Cannot delete 'main' branch")
        for row in self._branches.values():
            if row.branch.parent_branch_id == branch_id:
                raise ValueError(f"Cannot delete branch {branch_id} because branch {row.branch.branch_id} depends on it")

        self._branches.pop(branch_id, None)
        to_delete = [cp_id for cp_id, cp in self._checkpoints_by_id.items() if cp.branch_id == branch_id]
        for cp_id in to_delete:
            self._checkpoints_by_id.pop(cp_id, None)

    def reset(self) -> None:
        self._branches.clear()
        self._checkpoints_by_id.clear()
        self._branches["main"] = _BranchRow(
            branch=Branch(
                branch_id="main", parent_checkpoint_id=None, parent_branch_id=None,
                created_at_ns=0, seed_offset=0, head_seq_num=0, metadata={},
            ),
            checkpoints={}, name="main"
        )

    def _resolve_lineage(self, branch_id: str) -> list[tuple[str, int, int]]:
        """Walk parent chain to (branch_id, start_seq_exclusive, end_seq) segments,
        root-first, mirroring PostgresChronoDAG._resolve_lineage."""
        segments: list[tuple[str, int, int]] = []
        cursor = branch_id
        while cursor is not None:
            row = self._branches[cursor]
            fork_point = row.branch.parent_checkpoint_id
            start = self._checkpoints_by_id[fork_point].event_number if fork_point else 0
            segments.append((cursor, start, row.branch.head_seq_num))
            cursor = row.branch.parent_branch_id
        segments.reverse()
        return segments

    def checkout(self, branch_id: str) -> ReplayContext:
        row = self._branches[branch_id]
        if row.checkpoints:
            latest_num = max(row.checkpoints)
            checkpoint = row.checkpoints[latest_num]
        elif row.branch.parent_checkpoint_id:
            checkpoint = self._checkpoints_by_id[row.branch.parent_checkpoint_id]
        else:
            checkpoint = Checkpoint(
                checkpoint_id="genesis", branch_id=branch_id, event_number=0,
                sim_time_ns=0.0, state_hash="", aggregate_snapshot=b"", rng_state=b"",
            )
        pending = tuple(e for e in row.events if e.seq_num > checkpoint.event_number)
        return ReplayContext(branch=row.branch, checkpoint=checkpoint, pending_events=pending)

    def replay(self, branch_id: str, from_event: int, to_event: int) -> list[StoredEvent]:
        out: list[StoredEvent] = []
        for seg_branch, start, end in self._resolve_lineage(branch_id):
            lo, hi = max(from_event, start), min(to_event, end)
            if lo > hi:
                continue
            for e in self._branches[seg_branch].events:
                if lo < e.seq_num <= hi:
                    out.append(e)
        out.sort(key=lambda e: e.seq_num)
        return out

    def get_state_hash(self, branch_id: str, event_number: int) -> str:
        events = self.replay(branch_id, 0, event_number)
        raw = "|".join(f"{e.seq_num}:{e.event_id}:{e.event_type}" for e in events)
        return hashlib.sha256(raw.encode()).hexdigest()

    def diff(self, branch_a: str, branch_b: str, at_event: int) -> StateDiff:
        events_a = self.replay(branch_a, 0, at_event)
        events_b = self.replay(branch_b, 0, at_event)
        keys_a = {e.event_id for e in events_a}
        keys_b = {e.event_id for e in events_b}
        only_a = [e for e in events_a if e.event_id not in keys_b]
        only_b = [e for e in events_b if e.event_id not in keys_a]

        def as_entity_diff(e: StoredEvent) -> EntityDiff:
            return EntityDiff(
                entity_type=e.event_type,
                entity_id=e.event_id,
                changes=(FieldDelta("payload", None, e.payload),),
            )

        return StateDiff(
            branch_a_id=branch_a,
            branch_b_id=branch_b,
            at_event=at_event,
            entities_added=tuple(as_entity_diff(e) for e in only_b),
            entities_removed=(),
            entities_modified=tuple(as_entity_diff(e) for e in only_a),
            events_only_in_a=len(only_a),
            events_only_in_b=len(only_b),
        )

    # ── extras (not in the ChronoDAG protocol; UI-only convenience) ────

    def list_branches(self) -> list[_BranchRow]:
        return list(self._branches.values())

    def get_branch_row(self, branch_id: str) -> _BranchRow:
        return self._branches[branch_id]

    def list_events(self, branch_id: str, since_seq: int = 0, limit: int = 200) -> list[StoredEvent]:
        row = self._branches[branch_id]
        events = [e for e in row.events if e.seq_num > since_seq]
        return events[-limit:]
