# FinSim System Architecture

FinSim is a deterministic, event-sourced financial digital twin designed for payment network simulation, discrete-event scheduling, and branch-aware time travel.

---

## 1. Subsystem Decomposition & Ownership

As established in `initial_plan.md`, the platform is divided across three parallel contributors:

```
+-----------------------------------------------------------------------------+
|                               CONTRIBUTOR MAP                               |
+-------------------------------+-----------------------------+---------------+
|           Person 1            |          Person 2           |   Person 3    |
+-------------------------------+-----------------------------+---------------+
| * Core Engine (sim/core/)     | * Population (sim/pop/)     | * ChronoDAG   |
| * Gateway (sim/gateway/)      | * PaySim Calibration        | * Hashing     |
| * Scheduler (sim/scheduler/)  | * Behaviour Model           | * Obs         |
| * Repo, Config, CI            | * Agent Profiles & Temporal | * Tests       |
+-------------------------------+-----------------------------+---------------+
```

### Current Status

| Subsystem | Location | Status |
| :--- | :--- | :--- |
| **Core Contracts** | `sim/core/interfaces.py`, `sim/core/events.py` | Defined (Contracts) |
| **Core Engine** | `sim/core/engine.py`, `sim/core/*.py` | Pending (Person 1) |
| **Scheduler** | `sim/scheduler/rng.py`, `sim/scheduler/env.py` | Implemented (19 tests passing) |
| **Gateway Contracts** | `sim/gateway/interfaces.py` | Defined (Contracts) |
| **Gateway Implementation** | `sim/gateway/registry.py`, `sim/gateway/policy.py` | Pending (Person 1) |
| **Population Contracts** | `sim/population/interfaces.py` | Defined (Contracts) |
| **Population Implementation** | `sim/population/*.py`, `scripts/calibrate.py` | Pending (Person 2) |
| **Chrono Contracts** | `sim/chrono/interfaces.py` | Defined (Contracts) |
| **Chrono Implementation** | `sim/chrono/*.py` | Pending (Person 3) |
| **Observability** | `sim/observability/*.py` | Pending (Person 3) |

---

## 2. State & Event Causality Model (CQRS)

FinSim follows an event-sourced state transition model:

```
Intent (from BehaviourModel or Agent)
  |
  v
Command (validated by WorldEngine against in-memory aggregates)
  |
  v
DomainEvent(s) (emitted: PaymentRequested, AccountDebited, etc.)
  |
  v
StoredEvent (persisted to ChronoDAG with branch_id, seq_num, lineage)
  |
  v
apply_event (in-memory state projection updated: State' = Apply(State, Event))
```

- **In-Memory Projections**: Aggregates (`Account`, `Device`, `Merchant`, `Gateway`) are strictly updated by event handlers (`apply_event`).
- **Replay & Checkpoints**: Replay passes stored events through the same `apply_event` pipeline, ensuring identical deterministic state hashes.

---

## 3. Boundary Rules & Import Isolation

Configured in `pyproject.toml` and enforced via `import-linter` in CI:

1. Subsystems must only import from another subsystem's `interfaces.py` (or `sim/core/events.py`).
2. Direct imports of implementation modules across subsystem boundaries (e.g. `sim.core.engine` inside `sim/population/`) are forbidden.
3. No direct `simpy` imports outside `sim/scheduler/env.py`.
4. No unmanaged random calls (`random`, `numpy.random.seed()`) anywhere in the codebase.

---

## 4. Key Design Decisions

- **Currency**: INR only, represented as integer paise (`₹1.00 = 100 paise`).
- **Identifiers**: UUIDv7 time-ordered strings.
- **Simulation Time**: Discrete nanoseconds (`sim_time_ns: float`).
- **Roles**: `USER`, `MERCHANT`, `BANK_OPS`, `RISK_ANALYST`, `RED_AGENT`, `BLUE_AGENT`.
- **Transaction Types**: `PAYMENT`, `TRANSFER`, `CASH_IN`, `CASH_OUT`, `DEBIT`, `REFUND`, `CHARGEBACK`, `SETTLEMENT`, `FEE`, `INTEREST`.
- **WorldView Scope**: Each actor sees only their own accounts, registered devices, public merchant directory, and global parameters (no other users' data, no full graph).
