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
| **Population Implementation** | `sim/population/*.py`, `scripts/calibrate.py` | **Implemented (10 tests passing, Person 2)** |
| **Chrono Contracts** | `sim/chrono/interfaces.py` | Defined (Contracts) |
| **Chrono Implementation** | `sim/chrono/*.py` | Pending (Person 3) |
| **Observability** | `sim/observability/*.py` | Pending (Person 3) |

---

## 2. Population Subsystem Architecture (Person 2)

The population subsystem drives realistic behavioral activity in the discrete-event simulation:

```
data/paysim/ (CSVs)
      │
      ▼ (scripts/download_data.py & scripts/calibrate.py)
data/paysim/calibrated_params.json (CalibratedParams)
      │
      ├──► sim/population/profiles.py (ProfileSampler: lognormal amounts, repetition counts, balances)
      ├──► sim/population/temporal.py (TemporalModel: 24×7 diurnal rate matrices, Poisson inter-arrivals)
      │
      ▼
sim/population/behaviour.py (PopulationBehaviourModel: WorldView ──► Intent stream)
      │
      ▼
sim/population/agents.py (PopulationManager: batch entity spawning, public merchant directory)
```

### Implemented Modules

1. **`sim/population/calibration.py` & `scripts/calibrate.py`**:
   - Parses the 5 canonical PaySim CSVs (`clientsProfiles.csv`, `aggregatedTransactions.csv`, `initialBalancesDistribution.csv`, `maxOccurrencesPerClient.csv`, `transactionsTypes.csv`).
   - Converts currency into integer paise (`₹1.00 = 100 paise`).
   - Serializes/deserializes strongly-typed `CalibratedParams` to JSON.

2. **`sim/population/profiles.py` (`ProfileSampler`)**:
   - Samples action profiles weighted by empirical frequencies.
   - Samples lognormal transaction amounts in paise derived from arithmetic mean and standard deviation:
     $$\mu_{\ln} = \ln\left(\frac{\mu^2}{\sqrt{\mu^2 + \sigma^2}}\right), \quad \sigma_{\ln} = \sqrt{\ln\left(1 + \frac{\sigma^2}{\mu^2}\right)}$$
   - Samples piecewise initial balances and merchant category codes (MCC).

3. **`sim/population/temporal.py` (`TemporalModel`)**:
   - Maps discrete simulation nanoseconds (`sim_time_ns`) to `(day_of_week, hour_of_day)`.
   - Samples non-homogeneous Poisson process inter-arrival delays $\Delta t_{\text{ns}} \sim \text{Exp}(\lambda)$ where $\lambda$ is obtained from the $24 \times 7$ rate matrix.

4. **`sim/population/behaviour.py` (`PopulationBehaviourModel`)**:
   - Implements the `BehaviourModel` protocol.
   - Applies the **Balance-Spring Dynamic**: higher available balances increase outflow probabilities (`PAYMENT`, `TRANSFER`, `CASH_OUT`), while lower balances suppress outflows and favor inflows (`CASH_IN`, `DEBIT`). Zero balance produces zero outflows.
   - Generates deterministic UUIDv7 `idempotency_key` strings for 100% reproducible intent streams.

5. **`sim/population/agents.py` (`PopulationManager`)**:
   - Batch initializes `AgentEntity` records for retail users and merchants.
   - Generates public `MerchantDirectoryEntry` tuples for user world-views.

6. **Test Suites (`sim/population/tests/`)**:
   - `contract_test_population.py`: Protocol adherence, intent shape contracts, WorldView scope immutability, balance-spring dynamic, deterministic seed reproducibility, serialization fidelity, and strict import isolation.
   - `test_behaviour.py` & `test_agents.py`: Unit tests for profile sampling and agent management.

---

## 3. State & Event Causality Model (CQRS)

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

## 4. Boundary Rules & Import Isolation

Configured in `pyproject.toml` and enforced via `import-linter` in CI:

1. Subsystems must only import from another subsystem's `interfaces.py` (or `sim/core/events.py`).
2. Direct imports of implementation modules across subsystem boundaries (e.g. `sim.core.engine` inside `sim/population/`) are forbidden.
3. No direct `simpy` imports outside `sim/scheduler/env.py`.
4. No unmanaged random calls (`random`, `numpy.random.seed()`) anywhere in the codebase.

---

## 5. Key Design Decisions

- **Currency**: INR only, represented as integer paise (`₹1.00 = 100 paise`).
- **Identifiers**: UUIDv7 time-ordered strings.
- **Simulation Time**: Discrete nanoseconds (`sim_time_ns: float`).
- **Roles**: `USER`, `MERCHANT`, `BANK_OPS`, `RISK_ANALYST`, `RED_AGENT`, `BLUE_AGENT`.
- **Transaction Types**: `PAYMENT`, `TRANSFER`, `CASH_IN`, `CASH_OUT`, `DEBIT`, `REFUND`, `CHARGEBACK`, `SETTLEMENT`, `FEE`, `INTEREST`.
- **WorldView Scope**: Each actor sees only their own accounts, registered devices, public merchant directory, and global parameters (no other users' data, no full graph).
