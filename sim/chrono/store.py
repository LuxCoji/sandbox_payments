import json
import uuid

import psycopg

from sim.chrono.interfaces import (
    Branch,
    Checkpoint,
    ChronoDAG,
    EntityDiff,
    FieldDelta,
    ReplayContext,
    StateDiff,
    StoredEvent,
)


class PostgresChronoDAG(ChronoDAG):
    """PostgreSQL-backed implementation of the ChronoDAG."""

    def __init__(self, conn_info: str) -> None:
        self.conn_info = conn_info
        # For simplicity in testing, autocommit=True. In prod, we'd use connection pooling.
        self.conn = psycopg.connect(conn_info, autocommit=True)
        self._setup_db()

    def _setup_db(self) -> None:
        """Initialize the database schema for the branch-aware DAG."""
        with self.conn.cursor() as cur:
            cur.execute(
                '''
                CREATE TABLE IF NOT EXISTS branches (
                    branch_id TEXT PRIMARY KEY,
                    parent_branch_id TEXT,
                    parent_checkpoint_id TEXT,
                    created_at_ns BIGINT NOT NULL,
                    seed_offset INT NOT NULL,
                    head_seq_num INT NOT NULL,
                    metadata JSONB
                )
                '''
            )
            cur.execute(
                '''
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    branch_id TEXT NOT NULL REFERENCES branches(branch_id),
                    event_number INT NOT NULL,
                    sim_time_ns BIGINT NOT NULL,
                    state_hash TEXT NOT NULL,
                    aggregate_snapshot BYTEA NOT NULL,
                    rng_state BYTEA NOT NULL,
                    metadata JSONB
                )
                '''
            )
            cur.execute(
                '''
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    branch_id TEXT NOT NULL REFERENCES branches(branch_id),
                    seq_num INT NOT NULL,
                    event_type TEXT NOT NULL,
                    sim_time_ns BIGINT NOT NULL,
                    actor_id TEXT,
                    payload JSONB NOT NULL,
                    causation_id TEXT,
                    correlation_id TEXT
                )
                '''
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_branch_seq ON events(branch_id, seq_num)"
            )
            
            # Ensure 'main' root branch exists
            cur.execute("SELECT 1 FROM branches WHERE branch_id = 'main'")
            if not cur.fetchone():
                cur.execute(
                    '''
                    INSERT INTO branches (
                        branch_id, parent_branch_id, parent_checkpoint_id, 
                        created_at_ns, seed_offset, head_seq_num, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''',
                    ("main", None, None, 0, 0, 0, json.dumps({}))
                )

    def save_event(self, event: StoredEvent) -> None:
        """Append an event to the current branch log."""
        with self.conn.cursor() as cur:
            cur.execute(
                '''
                INSERT INTO events (
                    event_id, branch_id, seq_num, event_type, sim_time_ns,
                    actor_id, payload, causation_id, correlation_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    event.event_id,
                    event.branch_id,
                    event.seq_num,
                    event.event_type,
                    event.sim_time_ns,
                    event.actor_id,
                    json.dumps(event.payload),
                    event.causation_id,
                    event.correlation_id,
                )
            )
            # Fast-forward the branch head if this is the newest event
            cur.execute(
                "UPDATE branches SET head_seq_num = %s WHERE branch_id = %s AND head_seq_num < %s",
                (event.seq_num, event.branch_id, event.seq_num)
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
        """Capture state snapshot at the given event number."""
        checkpoint_id = str(uuid.uuid4())
        
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            branch_id=branch_id,
            event_number=event_number,
            sim_time_ns=sim_time_ns,
            state_hash=state_hash,
            aggregate_snapshot=aggregate_snapshot,
            rng_state=rng_state,
            metadata=metadata or {},
        )
        
        with self.conn.cursor() as cur:
            cur.execute(
                '''
                INSERT INTO checkpoints (
                    checkpoint_id, branch_id, event_number, sim_time_ns,
                    state_hash, aggregate_snapshot, rng_state, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    checkpoint.checkpoint_id,
                    checkpoint.branch_id,
                    checkpoint.event_number,
                    checkpoint.sim_time_ns,
                    checkpoint.state_hash,
                    checkpoint.aggregate_snapshot,
                    checkpoint.rng_state,
                    json.dumps(checkpoint.metadata),
                )
            )
        return checkpoint

    def _resolve_lineage(self, branch_id: str) -> list[tuple[str, int, int]]:
        """
        Helper method to resolve the lineage of a branch.
        Returns a list of (branch_id, start_seq_num, end_seq_num) ordered from oldest ancestor to the given branch.
        """
        lineage = []
        current_branch = branch_id
        
        with self.conn.cursor() as cur:
            cur.execute("SELECT head_seq_num FROM branches WHERE branch_id = %s", (current_branch,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Branch {branch_id} not found")
            current_end = row[0]
            
            while True:
                cur.execute(
                    '''
                    SELECT b.parent_branch_id, c.event_number
                    FROM branches b
                    LEFT JOIN checkpoints c ON b.parent_checkpoint_id = c.checkpoint_id
                    WHERE b.branch_id = %s
                    ''', (current_branch,)
                )
                parent_info = cur.fetchone()
                if not parent_info:
                    break
                    
                parent_branch_id, fork_event_number = parent_info
                
                if parent_branch_id is None:
                    # Root branch ('main')
                    lineage.append((current_branch, 0, current_end))
                    break
                else:
                    lineage.append((current_branch, fork_event_number + 1, current_end))
                    current_branch = parent_branch_id
                    current_end = fork_event_number
                    
        lineage.reverse()
        return lineage

    def fork(
        self,
        checkpoint_id: str,
        branch_id: str,
        metadata: dict[str, object] | None = None,
    ) -> Branch:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT branch_id, event_number, sim_time_ns FROM checkpoints WHERE checkpoint_id = %s",
                (checkpoint_id,)
            )
            cp_info = cur.fetchone()
            if not cp_info:
                raise ValueError(f"Checkpoint {checkpoint_id} not found")
                
            parent_branch_id, fork_event_number, created_at_ns = cp_info
            
            # Simple deterministic seed_offset derivation based on the branch name hash
            seed_offset = hash(branch_id) % (2**31 - 1)
            
            branch = Branch(
                branch_id=branch_id,
                parent_checkpoint_id=checkpoint_id,
                parent_branch_id=parent_branch_id,
                created_at_ns=created_at_ns,
                seed_offset=seed_offset,
                head_seq_num=fork_event_number,
                metadata=metadata or {},
            )
            
            cur.execute(
                '''
                INSERT INTO branches (
                    branch_id, parent_branch_id, parent_checkpoint_id,
                    created_at_ns, seed_offset, head_seq_num, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    branch.branch_id,
                    branch.parent_branch_id,
                    branch.parent_checkpoint_id,
                    branch.created_at_ns,
                    branch.seed_offset,
                    branch.head_seq_num,
                    json.dumps(branch.metadata)
                )
            )
            return branch

    def checkout(self, branch_id: str) -> ReplayContext:
        """Restore state from the latest checkpoint on a branch and return pending events to replay."""
        lineage = self._resolve_lineage(branch_id)
        latest_cp = None
        
        with self.conn.cursor() as cur:
            # Look backwards through the lineage for the most recent checkpoint
            for branch, b_start, b_end in reversed(lineage):
                cur.execute(
                    '''
                    SELECT checkpoint_id, branch_id, event_number, sim_time_ns, 
                           state_hash, aggregate_snapshot, rng_state, metadata
                    FROM checkpoints
                    WHERE branch_id = %s AND event_number <= %s
                    ORDER BY event_number DESC
                    LIMIT 1
                    ''',
                    (branch, b_end)
                )
                row = cur.fetchone()
                if row:
                    latest_cp = Checkpoint(
                        checkpoint_id=row[0],
                        branch_id=row[1],
                        event_number=row[2],
                        sim_time_ns=row[3],
                        state_hash=row[4],
                        aggregate_snapshot=row[5],
                        rng_state=row[6],
                        metadata=row[7]
                    )
                    break
            
            if not latest_cp:
                raise ValueError(f"No checkpoint found in lineage for {branch_id}")
                
            # Fetch branch details
            cur.execute("SELECT parent_checkpoint_id, parent_branch_id, created_at_ns, seed_offset, head_seq_num, metadata FROM branches WHERE branch_id = %s", (branch_id,))
            b_row = cur.fetchone()
            branch_obj = Branch(
                branch_id=branch_id,
                parent_checkpoint_id=b_row[0],
                parent_branch_id=b_row[1],
                created_at_ns=b_row[2],
                seed_offset=b_row[3],
                head_seq_num=b_row[4],
                metadata=b_row[5]
            )
            
            # Replay any events AFTER the checkpoint on this branch lineage
            pending_events = tuple(self.replay(branch_id, latest_cp.event_number + 1, branch_obj.head_seq_num))
            
            return ReplayContext(
                branch=branch_obj,
                checkpoint=latest_cp,
                pending_events=pending_events
            )

    def diff(self, branch_a: str, branch_b: str, at_event: int) -> StateDiff:
        """Compute recursive delta between states of two branches at a specific event."""
        with self.conn.cursor() as cur:
            def fetch_snapshot(branch_id: str) -> dict | None:
                cur.execute("SELECT aggregate_snapshot FROM checkpoints WHERE branch_id = %s AND event_number = %s", (branch_id, at_event))
                row = cur.fetchone()
                if row:
                    return json.loads(row[0])
                # Check lineage if not found directly
                for branch, b_start, b_end in self._resolve_lineage(branch_id):
                    if b_start <= at_event <= b_end:
                        cur.execute("SELECT aggregate_snapshot FROM checkpoints WHERE branch_id = %s AND event_number = %s", (branch, at_event))
                        row = cur.fetchone()
                        if row: return json.loads(row[0])
                return None
                
            state_a = fetch_snapshot(branch_a)
            state_b = fetch_snapshot(branch_b)
            
            if state_a is None or state_b is None:
                raise ValueError(f"Checkpoints must exist on both branches at event {at_event} to compute diff.")
                
        entities_added = []
        entities_removed = []
        entities_modified = []
        
        # Assume state is dict[entity_type, dict[entity_id, dict[field, value]]]
        all_entity_types = set(state_a.keys()).union(state_b.keys())
        
        for entity_type in all_entity_types:
            dict_a = state_a.get(entity_type, {})
            dict_b = state_b.get(entity_type, {})
            
            for eid in set(dict_a.keys()).union(dict_b.keys()):
                if eid in dict_b and eid not in dict_a:
                    entities_added.append(EntityDiff(entity_type, eid, ()))
                elif eid in dict_a and eid not in dict_b:
                    entities_removed.append(EntityDiff(entity_type, eid, ()))
                else:
                    obj_a, obj_b = dict_a[eid], dict_b[eid]
                    if obj_a != obj_b:
                        changes = []
                        for f in set(obj_a.keys()).union(obj_b.keys()):
                            val_a, val_b = obj_a.get(f), obj_b.get(f)
                            if val_a != val_b:
                                changes.append(FieldDelta(f, val_a, val_b))
                        entities_modified.append(EntityDiff(entity_type, eid, tuple(changes)))

        # Compare event lineages up to at_event
        events_a = {e.event_id for e in self.replay(branch_a, 0, at_event)}
        events_b = {e.event_id for e in self.replay(branch_b, 0, at_event)}
        
        return StateDiff(
            branch_a_id=branch_a,
            branch_b_id=branch_b,
            at_event=at_event,
            entities_added=tuple(entities_added),
            entities_removed=tuple(entities_removed),
            entities_modified=tuple(entities_modified),
            events_only_in_a=len(events_a - events_b),
            events_only_in_b=len(events_b - events_a)
        )

    def replay(self, branch_id: str, from_event: int, to_event: int) -> list[StoredEvent]:
        """Retrieve a range of events from a branch, correctly resolving lineage."""
        lineage = self._resolve_lineage(branch_id)
        events = []
        
        with self.conn.cursor() as cur:
            for branch, b_start, b_end in lineage:
                # Find the intersection/overlap between the requested range and this branch's segment
                overlap_start = max(from_event, b_start)
                overlap_end = min(to_event, b_end)
                
                if overlap_start <= overlap_end:
                    cur.execute(
                        '''
                        SELECT event_id, seq_num, event_type, sim_time_ns, actor_id, 
                               payload, causation_id, correlation_id
                        FROM events
                        WHERE branch_id = %s AND seq_num >= %s AND seq_num <= %s
                        ORDER BY seq_num ASC
                        ''',
                        (branch, overlap_start, overlap_end)
                    )
                    for row in cur.fetchall():
                        events.append(StoredEvent(
                            event_id=row[0],
                            branch_id=branch,
                            seq_num=row[1],
                            event_type=row[2],
                            sim_time_ns=row[3],
                            actor_id=row[4],
                            payload=row[5] if isinstance(row[5], dict) else json.loads(row[5]),
                            causation_id=row[6],
                            correlation_id=row[7]
                        ))
        return events

    def get_state_hash(self, branch_id: str, event_number: int) -> str:
        """Return SHA-256 state digest at a specific event on a branch."""
        lineage = self._resolve_lineage(branch_id)
        with self.conn.cursor() as cur:
            for branch, b_start, b_end in lineage:
                if b_start <= event_number <= b_end:
                    cur.execute(
                        "SELECT state_hash FROM checkpoints WHERE branch_id = %s AND event_number = %s",
                        (branch, event_number)
                    )
                    row = cur.fetchone()
                    if row:
                        return row[0]
            raise ValueError(f"No checkpoint found at event {event_number} for branch {branch_id}")
