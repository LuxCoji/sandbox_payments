"""In-memory ChronoDAG test double.

Mirrors PostgresChronoDAG's branch-lineage/checkpoint/diff/replay semantics
exactly (same algorithms, dict-backed instead of SQL-backed) so integration
tests exercise real fork/replay/diff behavior without requiring a live
Postgres instance.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

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


class InMemoryChronoDAG:
    """Faithful in-memory implementation of the ChronoDAG protocol."""

    def __init__(self) -> None:
        self._branches: dict[str, Branch] = {
            "main": Branch(
                branch_id="main", parent_checkpoint_id=None, parent_branch_id=None,
                created_at_ns=0, seed_offset=0, head_seq_num=0, metadata={},
            )
        }
        self._checkpoints: dict[str, Checkpoint] = {}
        self._events: list[StoredEvent] = []

    # ── Writes ───────────────────────────────────────────────────────────

    def save_event(self, event: StoredEvent) -> None:
        self.save_events([event])

    def save_events(self, events: list[StoredEvent]) -> None:
        for event in events:
            self._events.append(event)

        if events:
            last_event = max(events, key=lambda e: e.seq_num)
            branch = self._branches[last_event.branch_id]
            self._branches[last_event.branch_id] = Branch(
                branch_id=branch.branch_id,
                parent_checkpoint_id=branch.parent_checkpoint_id,
                parent_branch_id=branch.parent_branch_id,
                created_at_ns=branch.created_at_ns,
                seed_offset=branch.seed_offset,
                head_seq_num=max(branch.head_seq_num, last_event.seq_num),
                metadata=branch.metadata,
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
        checkpoint = Checkpoint(
            checkpoint_id=str(uuid.uuid4()),
            branch_id=branch_id,
            event_number=event_number,
            sim_time_ns=sim_time_ns,
            state_hash=state_hash,
            aggregate_snapshot=aggregate_snapshot,
            rng_state=rng_state,
            metadata=metadata or {},
        )
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def fork(
        self,
        checkpoint_id: str,
        branch_id: str,
        metadata: dict[str, object] | None = None,
    ) -> Branch:
        cp = self._checkpoints.get(checkpoint_id)
        if cp is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")

        branch = Branch(
            branch_id=branch_id,
            parent_checkpoint_id=checkpoint_id,
            parent_branch_id=cp.branch_id,
            created_at_ns=cp.sim_time_ns,
            seed_offset=hash(branch_id) % (2**31 - 1),
            head_seq_num=cp.event_number,
            metadata=metadata or {},
        )
        self._branches[branch_id] = branch
        return branch

    def delete_branch(self, branch_id: str) -> None:
        if branch_id == "main":
            raise ValueError("Cannot delete 'main' branch")
        for branch in self._branches.values():
            if branch.parent_branch_id == branch_id:
                raise ValueError(f"Cannot delete branch {branch_id} because branch {branch.branch_id} depends on it")

        self._branches.pop(branch_id, None)
        to_delete = [cp_id for cp_id, cp in self._checkpoints.items() if cp.branch_id == branch_id]
        for cp_id in to_delete:
            self._checkpoints.pop(cp_id, None)
        self._events = [e for e in self._events if e.branch_id != branch_id]

    def update_branch_metadata(self, branch_id: str, metadata: dict[str, object]) -> Branch:
        existing = self._branches.get(branch_id)
        if existing is None:
            raise ValueError(f"Branch {branch_id!r} not found")
        updated = Branch(
            branch_id=existing.branch_id,
            parent_checkpoint_id=existing.parent_checkpoint_id,
            parent_branch_id=existing.parent_branch_id,
            created_at_ns=existing.created_at_ns,
            seed_offset=existing.seed_offset,
            head_seq_num=existing.head_seq_num,
            metadata=metadata,
        )
        self._branches[branch_id] = updated
        return updated

    def reset(self) -> None:
        self._branches.clear()
        self._checkpoints.clear()
        self._events.clear()
        self._branches["main"] = Branch(
            branch_id="main", parent_checkpoint_id=None, parent_branch_id=None,
            created_at_ns=0, seed_offset=0, head_seq_num=0, metadata={},
        )

    # ── Lineage resolution (mirrors PostgresChronoDAG._resolve_lineage) ────

    def _resolve_lineage(self, branch_id: str) -> list[tuple[str, int, int]]:
        lineage: list[tuple[str, int, int]] = []
        current_branch = branch_id
        if current_branch not in self._branches:
            raise ValueError(f"Branch {branch_id} not found")
        current_end = self._branches[current_branch].head_seq_num

        while True:
            branch = self._branches.get(current_branch)
            if branch is None:
                break
            parent_branch_id = branch.parent_branch_id
            fork_event_number = None
            if branch.parent_checkpoint_id is not None:
                parent_cp = self._checkpoints.get(branch.parent_checkpoint_id)
                fork_event_number = parent_cp.event_number if parent_cp else None

            if parent_branch_id is None:
                lineage.append((current_branch, 0, current_end))
                break
            else:
                lineage.append((current_branch, (fork_event_number or 0) + 1, current_end))
                current_branch = parent_branch_id
                current_end = fork_event_number or 0

        lineage.reverse()
        return lineage

    # ── Reads ────────────────────────────────────────────────────────────

    def checkout(self, branch_id: str) -> ReplayContext:
        lineage = self._resolve_lineage(branch_id)
        latest_cp: Checkpoint | None = None
        for branch, _start, b_end in reversed(lineage):
            candidates = [
                cp for cp in self._checkpoints.values()
                if cp.branch_id == branch and cp.event_number <= b_end
            ]
            if candidates:
                latest_cp = max(candidates, key=lambda c: c.event_number)
                break
        if latest_cp is None:
            raise ValueError(f"No checkpoint found in lineage for {branch_id}")

        branch_obj = self._branches[branch_id]
        pending = tuple(self.replay(branch_id, latest_cp.event_number + 1, branch_obj.head_seq_num))
        return ReplayContext(branch=branch_obj, checkpoint=latest_cp, pending_events=pending)

    def replay(self, branch_id: str, from_event: int, to_event: int) -> list[StoredEvent]:
        lineage = self._resolve_lineage(branch_id)
        out: list[StoredEvent] = []
        for branch, b_start, b_end in lineage:
            overlap_start = max(from_event, b_start)
            overlap_end = min(to_event, b_end)
            if overlap_start <= overlap_end:
                matching = [
                    e for e in self._events
                    if e.branch_id == branch and overlap_start <= e.seq_num <= overlap_end
                ]
                out.extend(sorted(matching, key=lambda e: e.seq_num))
        return out

    def get_state_hash(self, branch_id: str, event_number: int) -> str:
        lineage = self._resolve_lineage(branch_id)
        for branch, b_start, b_end in lineage:
            if b_start <= event_number <= b_end:
                candidates = [
                    cp for cp in self._checkpoints.values()
                    if cp.branch_id == branch and cp.event_number == event_number
                ]
                if candidates:
                    return candidates[0].state_hash
        raise ValueError(f"No checkpoint found at event {event_number} for branch {branch_id}")

    def diff(self, branch_a: str, branch_b: str, at_event: int) -> StateDiff:
        import json

        SnapshotDict = dict[str, dict[str, dict[str, object]]]

        def snapshot(branch_id: str) -> SnapshotDict | None:
            for cp in self._checkpoints.values():
                if cp.branch_id == branch_id and cp.event_number == at_event:
                    return dict(json.loads(cp.aggregate_snapshot))
            for branch, b_start, b_end in self._resolve_lineage(branch_id):
                if b_start <= at_event <= b_end:
                    for cp in self._checkpoints.values():
                        if cp.branch_id == branch and cp.event_number == at_event:
                            return dict(json.loads(cp.aggregate_snapshot))
            return None

        state_a, state_b = snapshot(branch_a), snapshot(branch_b)
        if state_a is None or state_b is None:
            raise ValueError(f"Checkpoints must exist on both branches at event {at_event} to compute diff.")

        entities_added, entities_removed, entities_modified = [], [], []
        for entity_type in set(state_a) | set(state_b):
            dict_a, dict_b = state_a.get(entity_type, {}), state_b.get(entity_type, {})
            for eid in set(dict_a) | set(dict_b):
                if eid in dict_b and eid not in dict_a:
                    entities_added.append(EntityDiff(entity_type, eid, ()))
                elif eid in dict_a and eid not in dict_b:
                    entities_removed.append(EntityDiff(entity_type, eid, ()))
                else:
                    obj_a, obj_b = dict_a[eid], dict_b[eid]
                    if obj_a != obj_b:
                        changes = [
                            FieldDelta(f, obj_a.get(f), obj_b.get(f))
                            for f in set(obj_a) | set(obj_b)
                            if obj_a.get(f) != obj_b.get(f)
                        ]
                        entities_modified.append(EntityDiff(entity_type, eid, tuple(changes)))

        events_a = {e.event_id for e in self.replay(branch_a, 0, at_event)}
        events_b = {e.event_id for e in self.replay(branch_b, 0, at_event)}

        return StateDiff(
            branch_a_id=branch_a, branch_b_id=branch_b, at_event=at_event,
            entities_added=tuple(entities_added), entities_removed=tuple(entities_removed),
            entities_modified=tuple(entities_modified),
            events_only_in_a=len(events_a - events_b), events_only_in_b=len(events_b - events_a),
        )
