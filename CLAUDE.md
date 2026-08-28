# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FinSim: a deterministic, event-sourced financial simulation engine (retail payments, calibrated from PaySim data) with branch-based time-travel (ChronoDAG), a capability-gated tool gateway for agent interaction, and full observability. Package name is `sim` (the `finsim` name in `pyproject.toml`/docs is historical — always `import sim.*`, run `python -m sim.main`).

Full design docs: `docs/architecture.md` (system design), `docs/interfaces.md` (cross-subsystem contracts), `docs/chrono_dag.md`, `docs/rng_design.md`. `initial_plan.md` is the original multi-contributor work plan — historical, not current status. `docs/audit_plan.md` is a fixed audit/remediation log from a past pass; useful for "why is it built this way" context on things it touched.

## Commands

```bash
make install       # uv sync --extra dev — NOT `uv sync --dev`, see Gotchas below
make test           # full suite: uv run pytest -xvs
make test-contract   # contract_test_*.py files only: pytest -k contract_test
make test-integration # tests/integration/ only
make lint            # ruff check . && lint-imports (import-linter contracts)
make typecheck        # mypy sim/
make calibrate          # scripts/calibrate.py -> data/paysim/calibrated_params.json
make run                 # scripts/run_simulation.py (thin wrapper, no CLI args)
make regress               # scripts/regression_test.py against baselines/hashes.json
```

Single test: `uv run pytest sim/core/tests/test_engine.py::test_transfer_success -q`

Real CLI (composition root, not the Makefile's `run` target):
```bash
uv run python -m sim.main run-seed --seed 42 --users 1000 --duration-hours 24
uv run python -m sim.main fork-branch --checkpoint <id> --branch red-team
uv run python -m sim.main replay-branch --branch main --to-event 5000
uv run python -m sim.main diff-branches --branch-a main --branch-b red-team --event 5000
```

To run the Web UI (V2 UI includes Reset Simulation and Delete Branch capabilities) and API for real-time monitoring:
```bash
uv run uvicorn api.main:app --reload --port 8000 # API Server
cd frontend && npm run dev                       # Vite Frontend
```

Requires `.env` with `FINSIM_DB_URL` (Postgres — Supabase in practice, not local docker) for anything that touches ChronoDAG; `OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS` are optional (Grafana Cloud). No local docker-compose stack — that was removed.

## Architecture

### Subsystem boundaries (enforced by import-linter, not just convention)

Five subsystems under `sim/`: `core` (engine, accounts, payments), `population` (agent behaviour, calibration), `chrono` (branch-aware event store), `gateway` (capability-gated tool API), `scheduler` (RNG + discrete-event queue, shared by all). Plus `observability` (tracing/metrics/logging), importable by everyone, imports nothing subsystem-specific.

Additionally, there are two outer layers:
- `api/`: FastAPI proxy that wraps `sim/` components and provides a WebSocket stream for the UI. (Not a simulation subsystem, imports `sim` but `sim` never imports `api`).
- `frontend/`: React/Vite web UI for real-time dashboarding.

**Cross-subsystem imports may only target `sim/<subsystem>/interfaces.py`** — never a concrete module (`sim.core.engine`, `sim.chrono.store`, etc.) from another subsystem. `make lint`'s `lint-imports` step enforces this via 5 contracts in `pyproject.toml`'s `[tool.importlinter]` and will fail the build on any violation. When adding a field/method that crosses a subsystem boundary, add it to that subsystem's `interfaces.py` first.

### Event pipeline (CQRS)

`WorldEngine.execute_command()` (in `sim/core/engine.py`) follows: **Validate → Emit domain event(s) → Persist to ChronoDAG → Apply to in-memory aggregates**. Domain events (`sim/core/events.py`) are the single source of truth; aggregates (`Account`, `Payment`, `Merchant`, `Device` in `sim/core/*.py`) mutate *only* via `apply_event()`, never directly. `WorldEngineImpl` takes an optional `chrono: ChronoDAG | None` — `None` is fine for isolated unit tests (nothing persists), but the real composition root (`sim/main.py::build_simulation`) always wires a real `PostgresChronoDAG`.

Genesis account creation (population bootstrapping) is *not* a `Command`/`TransactionType` — there's no "create account" transaction type. It goes through `WorldEngine.create_account()`, a separate method that still routes through the same Emit→Persist→Apply pipeline. `PopulationManager.create_population()` must be called with `engine=<engine>` for agents to actually exist in engine state; without it, `world_view.accounts` is empty for every agent.

### Determinism

Everything derives from `DeterministicRNG` (`sim/scheduler/rng.py`, wraps `numpy.SeedSequence`) — **never** use `numpy.random`, stdlib `random`, or `uuid.uuid4()` anywhere in `sim/` (import-linter and code review should catch `numpy.random`/`random`; `uuid.uuid4()` slips past linting since it's not import-based — audit it manually). Entity-scoped randomness goes through `rng.spawn_for_entity(entity_type, entity_id)`, keyed on lowercase `"user"`/`"merchant"` — always route through `PopulationBehaviourModel.get_entity_rng()` rather than spawning ad hoc, so an entity's stream is one continuous sequence across init and later behaviour instead of silently restarting. Engine-internal deterministic IDs (`event_id`, `tx_id`) use `uuid5` name-based derivation (`WorldEngineImpl._next_event_id`/`_next_tx_id`), not time-ordered UUIDv7 — both are equally deterministic, they just don't sort by time.

`WorldEngine.get_state_hash()` is the reproducibility contract: same seed + same commands → identical hash. If you touch anything in the execute_command path, re-run `make test` (the determinism/full-simulation integration tests will catch regressions) — and be suspicious of any non-deterministic call (wall clock, unseeded RNG, dict/set iteration order feeding into the hash) you might introduce.

### ChronoDAG (branching)

`sim/chrono/store.py::PostgresChronoDAG` implements branch-aware event sourcing: `save_event`, `create_checkpoint`, `fork` (branch from a checkpoint), `checkout` (restore + pending events), `diff` (compare two branches at a given event number — requires a checkpoint to exist on *both* branches at that exact `event_number`, it's not automatic), `replay`, `get_state_hash`, `delete_branch`, and `reset_store`. Postgres integration in CI and the ChronoDAG protocol explicitly support resetting and deletion now. Branch lineage resolution (`_resolve_lineage`) walks parent branches via `parent_checkpoint_id`/`parent_branch_id` to compute per-branch event-number segments — a forked branch's own new events continue the seq_num counter from the fork point, they don't restart at 0.

`sim/chrono/tests/_fake_dag.py::InMemoryChronoDAG` is a faithful dict-backed reimplementation of the same algorithms, used by integration tests instead of mocking `psycopg` — prefer it over ad hoc mocks when a test needs real fork/diff/replay semantics.

### Gateway

`ToolGatewayImpl.call_tool()` (`sim/gateway/gateway.py`) checks capabilities (`ActorContext.capabilities` vs `ToolSpec.required_capabilities`), rate limits, then dispatches to a registered handler and filters output fields by `ActorRole`. Known incomplete: the LangGraph adapter (`sim/gateway/adapters.py`) is a stub, and the rate limiter is simple per-step/per-day counters, not a token bucket, despite the docstring/plan calling for one.

## Gotchas (from hard-won debugging, not obvious from reading the code)

- **`uv sync --dev` is wrong** — `dev` is a `[project.optional-dependencies]` extra, not a PEP 735 dependency-group. `uv sync --dev` silently matches nothing and will *uninstall* pytest/mypy/ruff/black/import-linter/hypothesis if they were already installed (sync reconciles the env to exactly what's requested). Always use `uv sync --extra dev` or `make install`.
- **`contract_test_*.py` files need the `python_files` override** in `pyproject.toml`'s `[tool.pytest.ini_options]` to be collected at all — pytest's default glob (`test_*.py`/`*_test.py`) doesn't match that prefix. If you add a new `contract_test_*.py` file and it's not showing up in `make test-contract`, check that config is still there.
- **Event scheduling has been observed to grow superlinearly with simulation duration** at fixed population size (200 users: 6h → ~6k steps, 24h → ~800k steps — a 4x duration increase produced ~130x more events). Root cause not yet found; suspect the interarrival/temporal model (`sim/population/temporal.py`). Keep integration test durations short until this is understood, and be wary of long `--duration-hours` runs.
- `diff()` on `ChronoDAG` compares checkpoints at a specific `event_number` — if you want to compare "current head state" of two branches that have diverged by different event counts, you need to explicitly create a checkpoint on each branch tagged with the same `event_number` first; there's no "diff current heads" shortcut.
