# Financial Digital Twin + Adversarial Testing Environment

## Detailed Work Plan for Three Parallel Contributors

---

## 1. Repository Initialization (Person 1)

Person 1 creates the canonical repository and establishes the foundational structure that all contributors will build upon.

### Steps

1. **Create the repository** (e.g., `finsim` on GitHub/GitLab).
2. **Set up workspace manifest** (`pyproject.toml`) using `uv` as the unified Python package manager:
   - Define project metadata (name, version, description, Python version requirement $\ge 3.11$).
   - List runtime dependencies: `simpy`, `eventsourcing`, `numpy`, `pydantic`, `pydantic-settings`, `psycopg`, `networkx`, `torch-geometric`, `structlog`, `opentelemetry-*`, `prometheus-client`, `pymacaroons`, `pyyaml`.
   - Define a `dev` optional-dependency group for testing, linting, type-checking, and import linting (`pytest`, `pytest-asyncio`, `ruff`, `black`, `mypy`, `import-linter`).
   - Configure `pytest` (test paths, asyncio mode).
   - Configure `ruff`, `black`, `mypy`, and `import-linter` for code quality.
3. **Create the directory layout** (empty folders and `__init__.py` files):

```text
finsim/
├── .github/
│   └── workflows/
│       ├── ci.yml          # CI pipeline (test, lint, typecheck, contract tests)
│       └── regression.yml  # Seeded deterministic regression runs
├── .pre-commit-config.yaml
├── pyproject.toml
├── docker-compose.yml      # Optional: PostgreSQL, Jaeger, Prometheus, Grafana (for local dev)
├── Makefile                # Dev shortcuts (up, down, test, lint, calibrate, run, regress, install)
├── docs/
│   ├── architecture.md
│   ├── interfaces.md       # Source of truth for all contracts
│   ├── rng_design.md
│   └── chrono_dag.md
├── sim/
│   ├── __init__.py
│   ├── main.py             # Composition root (wires all subsystems)
│   ├── config.py           # Pydantic-settings model
│   │
│   ├── core/               # ← Person 1 primary ownership
│   │   ├── __init__.py
│   │   ├── engine.py
│   │   ├── payment.py
│   │   ├── account.py
│   │   ├── device.py
│   │   ├── gateway.py
│   │   ├── merchant.py
│   │   ├── settlement.py
│   │   ├── events.py       # Event definitions (shared with chrono)
│   │   ├── interfaces.py   # Contract exposed to population & chrono
│   │   └── tests/
│   │       ├── test_engine.py
│   │       ├── test_payment_lifecycle.py
│   │       └── contract_test_core.py   # Contract tests for core
│   │
│   ├── population/         # ← Person 2 primary ownership
│   │   ├── __init__.py
│   │   ├── agents.py
│   │   ├── behaviour.py
│   │   ├── calibration.py
│   │   ├── profiles.py
│   │   ├── temporal.py
│   │   ├── interfaces.py   # Contract exposed to core & chrono
│   │   └── tests/
│   │       ├── test_agents.py
│   │       ├── test_behaviour.py
│   │       └── contract_test_population.py
│   │
│   ├── chrono/             # ← Person 3 primary ownership
│   │   ├── __init__.py
│   │   ├── store.py
│   │   ├── branch.py
│   │   ├── checkpoint.py
│   │   ├── replay.py
│   │   ├── hashing.py
│   │   ├── interfaces.py   # Contract exposed to core & population
│   │   └── tests/
│   │       ├── test_branch.py
│   │       ├── test_replay.py
│   │       └── contract_test_chrono.py
│   │
│   ├── scheduler/          # Shared (all three depend on this)
│   │   ├── __init__.py
│   │   ├── env.py          # Thin wrapper around simpy.Environment
│   │   ├── rng.py          # DeterministicRNG using numpy.SeedSequence.spawn()
│   │   └── tests/
│   │       └── test_rng.py
│   │
│   ├── gateway/            # Shared (Person 1 maintains, Persons 2-3 consume)
│   │   ├── __init__.py
│   │   ├── registry.py
│   │   ├── capability.py
│   │   ├── policy.py
│   │   ├── adapters.py     # LangGraph adapter
│   │   ├── interfaces.py   # Contract exposed to all
│   │   └── tests/
│   │       └── test_gateway.py
│   │
│   └── observability/      # Shared (Person 3 maintains, all consume)
│       ├── __init__.py
│       ├── tracing.py
│       ├── metrics.py
│       └── logging.py
│
├── data/
│   ├── paysim/             # Calibration data (CSV files from PaySim)
│   │   ├── clientsProfiles.csv
│   │   ├── aggregatedTransactions.csv
│   │   ├── initialBalancesDistribution.csv
│   │   ├── maxOccurrencesPerClient.csv
│   │   └── transactionsTypes.csv
│   └── ieee_cis/           # Optional validation data
│
├── scripts/
│   ├── download_data.py
│   ├── calibrate.py
│   ├── run_simulation.py
│   └── regression_test.py
│
└── tests/
    ├── integration/
    │   ├── test_full_simulation.py
    │   ├── test_branching.py
    │   └── test_determinism.py
    └── conftest.py
```

4. **Add a minimal `Makefile`** with phony targets:
   - `up` / `down` – Start/stop Docker services (PostgreSQL, Jaeger, Prometheus, etc.).
   - `test` – Run the full test suite (`uv run pytest -xvs`).
   - `test-contract` – Run only contract-test suites.
   - `test-integration` – Run integration tests.
   - `lint` – Run `ruff check`.
   - `typecheck` – Run `mypy`.
   - `calibrate` – Run `scripts/calibrate.py`.
   - `run` – Run `scripts/run_simulation.py`.
   - `regress` – Run `scripts/regression_test.py`.
   - `install` – Run `uv sync --dev` (installs dev dependencies).
5. **Add `.pre-commit-config.yaml`** to enforce formatting, linting, and type-checking on every commit.
6. **Commit the empty scaffold** with a clear message:
   ```bash
   git commit -m "chore: init repo with workspace, contracts, docker, CI"
   ```

> **Note:** At this point, the repository contains only the directory structure, manifest files, and empty interface contracts. No functional code exists yet, but the contracts are present as placeholders to be filled in by each contributor.

---

## 2. Contract-First Development (Shared Understanding)

All three contributors must treat the `*.py` files under `sim/*/interfaces.py` as the **single source of truth** for subsystem interactions. These files contain:

- **Dataclasses** defining immutable view objects (e.g., `WorldView`, `AccountSnapshot`, `Intent`, `ToolSpec`, `ActorContext`, `Checkpoint`, `Branch`).
- **Protocol classes** (using `typing.Protocol`) declaring the required methods each subsystem must implement.
- **Invariants and documentation** (docstrings) clarifying expected behaviour, thread-safety, and performance characteristics.

### State & Event Causality Model (CQRS / Event-Driven State)

To guarantee 100% deterministic time-travel, replay invariance, and branch diffing, FinSim follows a strict **unidirectional state transition model**:

```
Command (Execute Intent / Tool Call)
  │
  ▼
Validate against current In-Memory State / Aggregates
  │
  ▼
Emit Domain Event(s) (e.g., PaymentRequested, AccountDebited)
  │
  ▼
Append Event(s) to ChronoDAG Store (Branch-aware Event Log)
  │
  ▼
Apply Event(s) to In-Memory Aggregates (State = Apply(State, Event))
```

- **Events are the Single Source of Truth**: In-memory aggregate projections are updated **strictly** by applying emitted events via event handlers (`apply_event`). No component mutates in-memory state outside the event pipeline.
- **Replay Invariance**: Resetting or checking out a branch simply loads the checkpoint snapshot and replays events through the exact same `apply_event` pipeline, guaranteeing identical state hashes.

### Rules for Maintaining Contracts

1. **No Implementation Leaks**: Only signatures, dataclass fields, and protocol methods appear in interface modules.
2. **Strict Ownership**:
   - **Person 1**: `sim/core/interfaces.py` (`WorldEngine`), `sim/gateway/interfaces.py` (`ToolGateway`).
   - **Person 2**: `sim/population/interfaces.py` (`BehaviourModel`, `CalibratedParams`).
   - **Person 3**: `sim/chrono/interfaces.py` (`ChronoDAG`).
   - **Shared Concrete Contract**: `sim/scheduler/rng.py` (`DeterministicRNG`) is a shared utility whose public API is treated as a contract.
3. **Modification Protocol**:
   - Update the corresponding contract test file in your `tests/` directory.
   - Maintain backward compatibility for consumers unless a coordinated version bump is planned.
   - Run the contract test suite (`make test-contract`) to ensure consumer compatibility.
4. **Enforced Boundary Isolation**:
   - Consumers only import interfaces and never concrete implementation modules from another subsystem.
   - Enforced in CI via `import-linter` (e.g., `from sim.core.engine import ...` is forbidden inside `sim/population/` or `sim/chrono/`; only `from sim.core.interfaces import ...` is allowed).

---

## 3. Shared Building Blocks

### 3.1 Deterministic RNG (`sim/scheduler/rng.py`)

- **Owner**: Person 1 (initial implementation; treated as a public utility).
- **Public Contract**: `DeterministicRNG`
  - `from_seed(seed: int) -> DeterministicRNG`
  - `spawn(*labels: str) -> list[DeterministicRNG]` – Creates independent child streams.
  - `spawn_for_entity(entity_type: str, entity_id: str) -> DeterministicRNG` – Entity-keyed stream using deterministic key derivation (e.g. hash-based seed derivation from master seed and entity ID) so that entities receive identical RNG streams across forks regardless of creation order.
  - `random()`, `normal(mu, sigma)`, `lognormal(mu, sigma)`, `poisson(lam)`, `choice(seq, p=None)`
  - `get_state() -> bytes` / `set_state(state: bytes)` – For snapshotting and branch restoration.
- **Subsystem Usage**:
  - `WorldEngine` (Person 1): Creates master RNG from global seed and spawns streams for scheduler events.
  - `PopulationManager` (Person 2): Receives a `DeterministicRNG` for each entity via `spawn_for_entity`.
  - `ChronoDAG` (Person 3): Captures RNG states inside each `Checkpoint` and restores them on branch checkout to ensure downstream reproducibility.
- **Contract Enforcement**: No global `numpy.random` or Python standard library `random` calls permitted anywhere in the codebase (enforced via linting rules).

### 3.2 Observability (`sim/observability/*`)

- **Owner**: Person 3.
- **Public Contract**:
  - `@traced` decorator (OpenTelemetry) to automatically generate function/method spans.
  - `Metrics` class providing counters (`events_processed`, `tool_calls`, `forks_created`), gauges (`scheduler_queue_size`), and histograms (`event_latency`).
  - `StructuredLogger` emitting JSON-encoded logs with trace and span IDs.
- **Subsystem Usage**:
  - Contributors decorate public entrypoints (e.g., `WorldEngine.execute_command`, `BehaviourModel.propose_actions`, `ChronoDAG.fork`) with `@traced`.
  - Increment metrics during event scheduling, tool executions, and checkpoint creation.
  - Emit `INFO` logs for state changes and `DEBUG` logs for internal transitions.
- **Contract Enforcement**: Observability has no dependencies beyond OpenTelemetry and structured logging libraries; it does not import subsystem-specific code.

### 3.3 Scheduler Wrapper (`sim/scheduler/env.py`)

- **Owner**: Person 1.
- **Public Contract**: `SimulationEnv` (wrapping `simpy.Environment` with discrete-event priority queue semantics)
  - `schedule(event: ScheduledEvent) -> None` – Inserts into priority queue ordered by `(time, priority, sequence)`.
  - `run(until: float | None = None) -> None` – Advances discrete-event simulation time to next scheduled events.
  - `peek() -> ScheduledEvent | None` – Inspects next event without removal.
  - `pop() -> ScheduledEvent` – Removes and returns next event.
  - `now: float` – Current simulation time (read-only).
  - `step_count: int` – Monotonic logical event step counter.
- **Subsystem Usage**:
  - `WorldEngine` owns the single `SimulationEnv` instance and manages event execution.
  - Population and Chrono subsystems schedule future discrete actions by passing `ScheduledEvent` objects to `WorldEngine.schedule_event`.
- **Contract Enforcement**: Direct imports of `simpy` outside `sim/scheduler/env.py` are strictly prohibited.

---

## 4. Detailed Work Breakdown per Contributor

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       CONTRIBUTOR MAP                                       │
├───────────────────────────────┬─────────────────────────────┬───────────────────────────────┤
│           Person 1            │          Person 2           │           Person 3            │
├───────────────────────────────┼─────────────────────────────┼───────────────────────────────┤
│ • Core Engine                 │ • Retail Population         │ • ChronoDAG (Branching Store) │
│ • Tool Gateway                │ • PaySim Calibration        │ • State Hashing & Diffing     │
│ • Composition Root & CLI      │ • Behaviour Model (DES)     │ • Observability Subsystem     │
│ • Scheduler & RNG             │ • Agent Profiles & Temporal │ • Regression Test Harness     │
│ • Repo Scaffolding & CI       │ • Population Manager        │ • Integration Test Suite      │
└───────────────────────────────┴─────────────────────────────┴───────────────────────────────┘
```

### Person 1 – Core Engine, Gateway, Composition Root, Shared Scheduler & RNG

**Owned Modules**: `sim/core/*`, `sim/gateway/*`, `sim/scheduler/*`, `sim/main.py`, `sim/config.py`, top-level scaffolding (`Makefile`, `CI`, `Docker`, `docs`).

#### Detailed Tasks

1. **Core Engine (`sim/core/`)**:
   - Define domain event dataclasses in `sim/core/events.py` (`PaymentRequested`, `PaymentAuthorized`, `PaymentSettled`, `AccountDebited`, `AccountCredited`, `DeviceRegistered`, `MerchantOnboarded`).
   - Implement `WorldEngine` protocol in `sim/core/engine.py`:
     - Holds `SimulationEnv` and in-memory aggregate state projections (`Accounts`, `Devices`, `Merchants`, `Gateways`).
     - `schedule_event(event)`: Translates domain actions into `ScheduledEvent` objects for the discrete-event scheduler.
     - `get_world_view(actor_id, actor_role)`: Builds and returns an immutable `WorldView` from current state, applying field-level masking based on role permissions.
     - `execute_command(cmd)`: Validates command against current aggregate state, generates domain events, appends them to ChronoDAG, and applies the events to mutate aggregate state via their respective `apply` handlers.
     - `get_state_hash()`: Computes deterministic SHA-256 digest of canonical state projections.
   - Implement financial lifecycle state machines in `sim/core/payment.py`, `account.py`, `device.py`, `merchant.py`, `gateway.py`, `settlement.py` as event-applied aggregates.
   - Write contract tests in `sim/core/tests/contract_test_core.py` (verify `WorldView` immutability, event ordering, PII masking, and state transition invariance).

2. **Gateway (`sim/gateway/`)**:
   - Define `ToolSpec` and `ActorContext` dataclasses in `sim/gateway/interfaces.py`.
   - Implement `ToolRegistry` in `sim/gateway/registry.py` (`register_tool`, `get_tool`, `list_tools`).
   - Implement policy & capability checks in `sim/gateway/policy.py`:
     - Capability authorization (`required_capability` match against `ActorContext.capabilities`).
     - Token-bucket rate limiting per actor and tool.
     - Output field visibility filtering based on actor role.
   - Implement `ToolGateway` in `sim/gateway/gateway.py` (`call_tool`).
   - Provide a LangGraph adapter in `sim/gateway/adapters.py` (translating agent states and executing tools within the simulation context).
   - Write contract tests in `sim/gateway/tests/contract_test_gateway.py`.

3. **Scheduler & RNG (`sim/scheduler/`)**:
   - Implement `DeterministicRNG` (`sim/scheduler/rng.py`) using `numpy.SeedSequence` with deterministic entity seed derivation.
   - Implement `SimulationEnv` wrapper (`sim/scheduler/env.py`) providing discrete-event queue ordering.
   - Write unit tests in `sim/scheduler/tests/test_rng.py` (verifying stream independence, determinism across forks, and state restoration).

4. **Composition Root (`sim/main.py`)**:
   - Read configuration via `sim/config.py` (Pydantic Settings).
   - Initialize PostgreSQL-backed event store application for ChronoDAG.
   - Instantiate `DeterministicRNG`, `SimulationEnv`, `WorldEngine`, `ToolGateway`, and Chrono store.
   - Register core simulation tools (`create_account`, `transfer_funds`, `make_payment`, `wait`, `inspect_account`).
   - Wire Population scheduling and Gateway tool routing.
   - Set up OpenTelemetry/Jaeger exporter and Prometheus `/metrics`.
   - Provide CLI entrypoint (`python -m finsim.main`) for `run-seed`, `fork-branch`, `replay-branch`, `diff-branches`.

5. **Documentation & CI**:
   - Maintain `docs/interfaces.md`.
   - Configure `.github/workflows/ci.yml` and `regression.yml`.

#### Deliverables
- Composable core engine with deterministic event execution and capability checking.
- Passing contract tests for core and gateway interfaces.
- Stable shared scheduler and deterministic RNG utilities.

---

### Person 2 – Retail Population, Calibration, Behaviour Model

**Owned Modules**: `sim/population/*`, `data/paysim/*`, `scripts/download_data.py`, `scripts/calibrate.py`.

#### Detailed Tasks

1. **Data Acquisition & Verification**:
   - Fetch PaySim dataset files via `scripts/download_data.py`: `clientsProfiles.csv`, `aggregatedTransactions.csv`, `initialBalancesDistribution.csv`, `maxOccurrencesPerClient.csv`, `transactionsTypes.csv`.
   - Store files under `data/paysim/` with validation checks.

2. **Calibration Pipeline (`sim/population/calibration.py` & `scripts/calibrate.py`)**:
   - Parse `clientsProfiles.csv` into `ActionProfile` instances (`min_count`, `max_count`, `avg_amount`, `std_amount`, `frequency`).
   - Derive categorical distribution over profiles per action type.
   - Parse `initialBalancesDistribution.csv` into piecewise distributions for starting balances.
   - Parse `maxOccurrencesPerClient.csv` to establish per-client action caps.
   - Export parameters to `calibrated_params.json` conforming to `CalibratedParams` dataclass.

3. **Profiles & Temporal Modeling (`sim/population/profiles.py`, `sim/population/temporal.py`)**:
   - `profiles.py`: Expose `sample_profile(action_type: str, rng: DeterministicRNG) -> ActionProfile`.
   - `temporal.py`: Model $24 \times 7$ rate matrices per action from temporal data. Expose `get_next_interarrival(action_type, current_time, rng) -> float` for discrete-event scheduling.

4. **Behaviour Model Implementation (`sim/population/behaviour.py`)**:
   - Implement `BehaviourModel` protocol.
   - `propose_actions(entity_id: str, world_view: WorldView) -> list[Intent]`:
     - **USER Entities**:
       - Apply balance-spring dynamic: higher balances increase probability of outflow actions (`PAYMENT`, `TRANSFER`, `CASH_OUT`), while lower balances favor inflow (`DEBIT`, `REFUND`).
       - Sample action type $\rightarrow$ profile $\rightarrow$ repetition count $\rightarrow$ log-normal amount.
       - Select targets (merchant affinity matrix for `PAYMENT`, peer accounts for `TRANSFER`, cash entity for `CASH_OUT`).
       - Construct `Intent` list.
     - **MERCHANT Entities**:
       - Model low-probability passive merchant actions (`REFUND`, `PAYOUT`) based on balance and temporal rates.
   - `initialize_entity(entity_id, entity_type, rng) -> dict`: Generates starting balance, KYC attributes, and registration metadata.

5. **Population Manager & DES Lifecycle (`sim/population/agents.py`)**:
   - Batch creation of $N$ user and $M$ merchant accounts via `WorldEngine.execute_command`.
   - Schedule initial and recurring agent actions in the discrete-event simulation queue based on sampled inter-arrival times $\Delta t$.

6. **Contract Tests (`sim/population/tests/contract_test_population.py`)**:
   - Validate intent output schema, balance-spring dynamic compliance, and entity initialization.
   - Verify deterministic reproducibility given matching RNG seeds.

#### Deliverables
- Calibrated, realistic population simulator generating compliant intent streams.
- Completed interface `sim/population/interfaces.py` and passing contract tests.
- Automated calibration and data processing pipeline.

---

### Person 3 – ChronoDAG, Observability, Regression Harness

**Owned Modules**: `sim/chrono/*`, `sim/observability/*`, `scripts/run_simulation.py`, `scripts/regression_test.py`, `tests/integration/*`.

#### Detailed Tasks

1. **Event Sourcing & ChronoDAG Backend (`sim/chrono/store.py`)**:
   - Build custom PostgreSQL-backed store supporting **branch-aware event lineages / DAG structures**.
   - Configure snapshotting strategy (e.g., aggregate snapshots every 100 events).
   - Implement `ChronoDAG` protocol methods:
     - `save_event(event: StoredEvent) -> None`: Appends event to current branch log.
     - `create_checkpoint(event_number: int) -> Checkpoint`: Captures state snapshot, canonical state hash, simulation timestamp, and RNG state bytes.
     - `fork(checkpoint_id: str, branch_id: str, metadata: dict) -> Branch`: Records branch lineage originating from parent checkpoint with derived seed offset.
     - `checkout(branch_id: str) -> ReplayContext`: Restores aggregate snapshot, repositions event cursor, and restores branch RNG state.
     - `diff(branch_a: str, branch_b: str, at_event: int) -> StateDiff`: Computes recursive delta between states of two branches at a specific event index.
     - `replay(branch_id: str, from_event: int, to_event: int) -> list[StoredEvent]`: Retrieves range of events from branch log.
     - `get_state_hash(branch_id: str, event_number: int) -> str`: Returns SHA-256 state digest.

2. **State Hashing (`sim/chrono/hashing.py`)**:
   - Implement deterministic canonical JSON serializer (lexicographically sorted keys, base-10 numbers, ISO-8601 timestamps).
   - Compute SHA-256 hash over canonical state representation.
   - Support incremental state hashing for modified aggregates.

3. **Observability (`sim/observability/*`)**:
   - `tracing.py`: OpenTelemetry `TracerProvider` with Jaeger OTLP export and `@traced` decorator.
   - `metrics.py`: Prometheus metrics (counters, gauges, histograms for latency and queue size).
   - `logging.py`: Structured JSON logger via `structlog` with trace/span ID enrichment.

4. **Simulation Runner (`scripts/run_simulation.py`)**:
   - CLI runner accepting seeds, durations, agent counts, and branch targets.
   - Initializes subsystems, advances discrete-event simulation, dispatches intents, and outputs final state hashes.

5. **Regression & Integration Harness (`scripts/regression_test.py`, `tests/integration/`)**:
   - `scripts/regression_test.py`: Evaluates a matrix of seeds against baseline hashes (`baselines/hashes.json`).
   - `tests/integration/test_full_simulation.py`: Multi-agent execution test.
   - `tests/integration/test_branching.py`: Fork injection test verifying branch isolation and diff accuracy.
   - `tests/integration/test_determinism.py`: Validates multi-run hash identity under matching seeds.

6. **Contract Tests (`sim/chrono/tests/contract_test_chrono.py`)**:
   - Verify fork RNG stream independence, replay/commit equivalence, and state hash invariance.

#### Deliverables
- High-performance ChronoDAG backend supporting branching, checkpointing, and replay.
- Observability infrastructure (traces, metrics, logs).
- Deterministic regression test harness and CI integration test suite.

---

## 5. How Contracts Are Enforced in Practice

### Import Linter Rules
Configured in `pyproject.toml` under `[tool.importlinter]`:
- **Subsystem Isolation**: Concrete modules in `sim.core`, `sim.population`, `sim.chrono`, and `sim.gateway` must never directly import from one another.
- **Interface Only**: Cross-subsystem imports must target `sim.<subsystem>.interfaces`.
- **Acyclic Dependency Graph**: Import cycles are forbidden and fail CI builds.

### Contract Test Suites
- Contract tests validate protocol conformance independently of specific implementation internals.
- Executed via `make test-contract` on every merge request.

### Shared Dependencies
- Utilities (`DeterministicRNG`, `SimulationEnv`, `observability`) expose strict public APIs without leaking implementation internals (e.g., no direct access to underlying `simpy` or `numpy.random.Generator`).

### Interface Versioning
- Modifications to existing contracts must remain backward-compatible.
- Breaking modifications require introducing versioned interface files (e.g., `interfaces_v2.py`) and coordinated deprecation.

---

## 6. Contributor Workflow (Using `uv` & Git Branches)

```mermaid
gitGraph
   commit id: "repo-init"
   branch feat/core-engine
   branch feat/population-model
   branch feat/chrono-dag
   checkout feat/core-engine
   commit id: "core-contracts"
   checkout feat/population-model
   commit id: "pop-calibration"
   checkout feat/chrono-dag
   commit id: "chrono-store"
   checkout main
   merge feat/core-engine id: "merge-core"
   checkout feat/population-model
   commit id: "rebase-main"
   checkout main
   merge feat/population-model id: "merge-pop"
   checkout feat/chrono-dag
   merge feat/chrono-dag id: "merge-chrono"
```

### 1. Development Cycle
```bash
# Checkout feature branch from main
git checkout -b feat/<subsystem-name> main

# Sync dependencies
uv sync --dev

# Run tests frequently during development
make lint
make typecheck
make test-contract
make test

# Push branch and create Pull Request
git push origin feat/<subsystem-name>
```

### 2. Updating Dependencies
- **Runtime dependencies**: Add to `[project.dependencies]` in `pyproject.toml`, run `uv lock`, and commit.
- **Development dependencies**: Add to `[project.optional-dependencies].dev` in `pyproject.toml`, run `uv lock`, and commit.

### 3. Staying Up-to-Date
- Frequently rebase feature branches against `main`:
  ```bash
  git fetch origin main
  git rebase origin/main
  ```

---

## 7. Definition of "Done" (Vertical Slice Baseline)

The project milestone is complete when the following criteria pass on `main`:

| Capability | Validation Criteria | Verification Method |
| :--- | :--- | :--- |
| **Deterministic Replay** | Identical seed yields exact state hash over 10,000 users and 100,000 events. | `make regress` / `scripts/regression_test.py` |
| **Branching Fidelity** | Forking at event 5,000 with 1,000 red-agent tool calls leaves main branch unmutated; diff reflects only altered aggregates. | `tests/integration/test_branching.py` |
| **Observability** | All events, tool invocations, and forks emit Jaeger trace spans; metrics exposed on `/metrics`. | Jaeger UI & Prometheus scrape check |
| **Contract Compliance** | 100% pass rate on contract test suites with zero `import-linter` violations. | `make test-contract` & `make lint` |
| **Calibration Integrity** | Action frequencies and temporal rates match PaySim within 95% confidence interval. | `tests/integration/test_population_calibration.py` |