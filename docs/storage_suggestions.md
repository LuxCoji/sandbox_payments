# FinSim Storage & Memory Optimization Suggestions

This document details the root causes of storage and memory consumption across the FinSim simulation platform and outlines concrete, non-breaking architectural proposals to optimize both **PostgreSQL disk storage** and **Python in-memory (RAM) heap**.

---

## 1. Storage & Memory Architecture Overview

FinSim operates a dual-layer architecture:
1. **In-Memory RAM Engine (`WorldEngineImpl`)**: Holds active mutable state (account balances, devices, merchants, future scheduler queue) for sub-microsecond validation during DES simulation ticks.
2. **PostgreSQL Event Store (`PostgresChronoDAG`)**: Maintains an immutable append-only event log, full state checkpoints, and branch lineage graphs for replay and multiverse branching.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           1. IN-MEMORY (RAM)                                │
│                     "The Fast Live State Engine"                            │
│                                                                             │
│  • Account balances, devices, merchants, gateways                           │
│  • Future Event Queue (Discrete-Event Scheduler in sim_time_ns)             │
│  • Random Number Generators (RNG)                                           │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                    Every action emits │ Periodic snapshots of
                         Domain Events │ aggregate state
                                       │
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                        2. POSTGRESQL (DATABASE)                             │
│                  "The Time-Machine / ChronoDAG Ledger"                      │
│                                                                             │
│  • events table: Immutable historical log (JSONB payload)                   │
│  • checkpoints table: Aggregate snapshots (BYTEA)                           │
│  • branches table: DAG timeline tree (main vs. red-team branches)           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Root Causes of Storage & Memory Consumption

### A. Uncompressed Checkpoint Snapshots (`BYTEA`)
- **Location**: `sim/chrono/store.py` (`PostgresChronoDAG.create_checkpoint`)
- **Mechanism**: Every checkpoint serializes all accounts, devices, merchants, and gateways into a raw UTF-8 JSON byte string stored in `checkpoints.aggregate_snapshot`.
- **Impact**: With 1,000 entities, each checkpoint is ~2–5 MB of uncompressed data. Saving dozens of checkpoints across multiple test branches rapidly consumes database storage.

### B. Event Sourcing Write Amplification
- **Location**: `sim/core/engine.py`
- **Mechanism**: A single retail payment generates 3 to 5 separate domain events (`PaymentRequested`, `AccountDebited`, `AccountCredited`, `PaymentAuthorized`, `PaymentSettled`).
- **Impact**: 50,000 user transactions produce 200,000+ database rows with JSONB payloads, primary keys, and B-tree indexes (`idx_events_branch_seq`).

### C. Superlinear Event Growth in Population Loops
- **Location**: `sim/population/temporal.py` & `sim/population/agents.py`
- **Mechanism**: On long runs without strict per-agent transaction rate caps, the Poisson rate sampling can trigger exponential event scheduling (e.g. 200 users running for 24h generated ~800,000 events).

### D. Unbounded In-Memory Event Accumulation in API
- **Location**: `api/live_dag.py` (`LiveChronoDAG`)
- **Mechanism**: The live WebSocket API server appends every event to an unbounded Python list (`row.events.append(event)`). Over prolonged runs, this increases Python process RAM indefinitely.

---

## 3. Concrete Optimization Proposals

### Proposal 1: Transparent Checkpoint Compression (PostgreSQL `BYTEA`)
**Target**: `sim/chrono/store.py` (`PostgresChronoDAG`)  
**Expected Savings**: **85%–90% reduction** in `checkpoints` table size.

Compress the canonical state bytes with `zlib` (standard library, level 6) before writing to Postgres, and decompress upon checkout and diff:

```python
import zlib

def _compress_snapshot(data: bytes) -> bytes:
    if not data:
        return data
    return zlib.compress(data, level=6)

def _decompress_snapshot(data: bytes) -> bytes:
    if not data:
        return data
    try:
        return zlib.decompress(data)
    except (zlib.error, ValueError):
        return data  # Fallback for existing uncompressed records
```

- **In `create_checkpoint()`**:
  ```python
  compressed_snapshot = _compress_snapshot(checkpoint.aggregate_snapshot)
  # INSERT INTO checkpoints (...) VALUES (..., compressed_snapshot, ...)
  ```
- **In `checkout()` & `diff()`**:
  ```python
  raw_snapshot = _decompress_snapshot(row[5])
  ```

---

### Proposal 2: Bounded Rolling Buffers for Live UI (`LiveChronoDAG`)
**Target**: `api/live_dag.py`  
**Expected Savings**: Capped, constant RAM footprint for the API server.

Replace unbounded event storage with a rolling window (e.g., latest 5,000–10,000 events per branch) sufficient for live UI feeds and diffs:

```python
# In LiveChronoDAG.save_events:
for event in events:
    row.events.append(event)
    self._broadcast(event)

if len(row.events) > 10000:
    row.events = row.events[-5000:]
```

---

### Proposal 3: Normalizing Per-Agent Transaction Frequency
**Target**: `sim/population/temporal.py`  
**Expected Savings**: **~90–95% reduction** in total generated simulation events.

Ensure that Poisson inter-arrival intervals are strictly bounded per agent based on real-world retail profiles (e.g., 2–5 transactions per user per day):
$$\lambda_{\text{user}} = \frac{\text{expected daily transactions}}{86,400 \times 10^9 \text{ ns}}$$

With 200 users:
$$200 \text{ users} \times 4 \text{ tx/day} \times 4 \text{ events/tx} \approx 3,200 \text{ events/day (vs. 800,000)}$$

---

### Proposal 4: Automated Scratch Branch Pruning
**Target**: `sim/chrono/store.py` / Red-Team Harness  
**Expected Savings**: Prevents test branches from polluting persistent storage.

Red-team exploratory sub-branches (`red-team/session-*/attempt-*`) should be automatically deleted once evaluated:
```python
dag.delete_branch(scratch_branch_id)
```
This triggers foreign key cascading cleanup of associated events and checkpoints.

---

### Proposal 5: Dataclass Object Overhead Reduction (`slots=True`)
**Target**: `sim/chrono/interfaces.py`, `sim/core/events.py`, `sim/scheduler/env.py`  
**Expected Savings**: **~60% less Python RAM** per stored/scheduled event.

Add `slots=True` to high-frequency immutable dataclasses:
```python
@dataclass(frozen=True, slots=True)
class StoredEvent:
    event_id: str
    event_type: str
    sim_time_ns: float
    actor_id: str
    branch_id: str
    seq_num: int
    payload: dict
    causation_id: str | None = None
    correlation_id: str | None = None
```

---

## 4. PostgreSQL Maintenance & Reclamation Runbook

When resetting or deleting test data, PostgreSQL uses MVCC and does not immediately shrink physical disk files. Use the following maintenance commands to immediately reclaim disk space:

```sql
-- 1. Truncate all tables and recreate the 'main' branch
TRUNCATE TABLE events, checkpoints, branches CASCADE;
INSERT INTO branches (
    branch_id, parent_branch_id, parent_checkpoint_id,
    created_at_ns, seed_offset, head_seq_num, metadata
) VALUES ('main', NULL, NULL, 0, 0, 0, '{}');

-- 2. Physically reclaim unallocated disk pages
VACUUM FULL events;
VACUUM FULL checkpoints;
VACUUM FULL branches;
```

---

## 5. Implementation Priority Matrix

| Initiative | Implementation Effort | Storage / RAM Impact | Risk Level |
| :--- | :--- | :--- | :--- |
| **Zlib Checkpoint Compression** | Low (< 20 lines) | **85–90% DB disk reduction** | Low (non-breaking) |
| **Bounded API Event Buffer** | Low (< 5 lines) | **Eliminates API RAM leak** | Low |
| **Temporal Rate Normalization** | Medium | **90–95% less DB write volume** | Medium (requires calibration check) |
| **Automatic Branch Cleanup** | Low | **Keeps database lean** | Low |
| **`slots=True` on Event Dataclasses** | Low | **60% Python heap savings** | Low |
