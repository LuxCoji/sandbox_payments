# FinSim – Financial Digital Twin + Adversarial Testing Environment

A deterministic, event-sourced financial simulation engine with branching time-travel, adversarial agent injection, and full observability.

## Overview

FinSim models a realistic retail payment ecosystem using discrete-event simulation, calibrated from real-world transaction data (PaySim). It supports:

- **Deterministic replay** – identical seeds produce identical state hashes
- **Branching (ChronoDAG)** – fork simulation state at any point, inject adversarial agents, and diff outcomes. Postgres integration in CI and the ChronoDAG protocol explicitly support resetting and deletion now.
- **Tool Gateway** – capability-gated, rate-limited API for agent interaction
- **Full observability** – OpenTelemetry traces, Prometheus metrics, structured logging

## Quick Start

### Prerequisites

- Python >= 3.11
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- A Postgres connection (Supabase, or any Postgres instance) for the ChronoDAG store
- (Optional) A Grafana Cloud OTLP endpoint for traces/metrics — the sim runs fine without it, you just won't see spans/metrics anywhere

Backing services are **hosted, not local**: Postgres via Supabase and
traces/metrics via Grafana Cloud (OTLP). There is no `make up`/`make down`
local-docker step — that was removed when the project migrated off
docker-compose.

### 1. Install dependencies

```bash
make install          # = uv sync --extra dev
```

`dev` is a `[project.optional-dependencies]` **extra**, not a PEP 735
dependency-group — always use `--extra dev` (or `make install`), never
`uv sync --dev`. `--dev` silently matches nothing here and `uv sync` will
then *uninstall* pytest/mypy/ruff/black/import-linter/hypothesis from
`.venv` since they're no longer part of what's requested. If you ever hit
`error: Failed to spawn: 'pytest'` (or mypy/ruff), that's what happened —
fix with `make install`.

### 2. Configure environment

Create a `.env` file in the repo root (auto-loaded via `python-dotenv`):

```bash
FINSIM_DB_URL="postgresql://<user>:<password>@<host>:5432/<database>"

# Optional — only needed if you want traces/metrics exported somewhere real
OTEL_EXPORTER_OTLP_ENDPOINT="https://<your-grafana-cloud-otlp-gateway>/otlp"
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64-token>"
```

`sim/config.py`'s `SimConfig` reads `FINSIM_DB_URL` via the `FINSIM_` env
prefix; extra unrecognized env vars (e.g. the `OTEL_*` ones above) are
ignored, not errors.

### 3. Generate calibration data

```bash
uv run python scripts/download_data.py   # synthesizes data/paysim/*.csv (gitignored, not fetched from a network)
make calibrate                           # optional: writes data/paysim/calibrated_params.json
```

### 4. Verify the install

```bash
make test        # full test suite (unit + contract + integration)
make lint         # ruff + import-linter
make typecheck    # mypy sim/
```

### 5. Run a simulation

The composition-root CLI lives in `sim/main.py` (`python -m sim.main`);
`scripts/run_simulation.py` and `make run` are thin wrappers around it.

```bash
# Run a new simulation from a deterministic seed
uv run python -m sim.main run-seed --seed 42 --users 1000 --duration-hours 24

# Fork an alternate-timeline branch from a checkpoint
uv run python -m sim.main fork-branch --checkpoint <checkpoint_id> --branch red-team

# Replay a branch's event log
uv run python -m sim.main replay-branch --branch main --to-event 5000

# Diff two branches at a specific event
uv run python -m sim.main diff-branches --branch-a main --branch-b red-team --event 5000

# Or via Makefile (defaults only, no CLI args):
make run
make regress      # deterministic regression suite against baselines/hashes.json
```

### 6. Run the Web UI & API

FinSim includes a FastAPI backend and a React/Vite frontend for real-time simulation monitoring. The new V2 UI features include Reset Simulation and Delete Branch capabilities.

```bash
# Terminal 1: Start the API server
uv run uvicorn api.main:app --reload --port 8000

# Terminal 2: Start the frontend UI
cd frontend
npm install
npm run dev
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full system design,
[docs/interfaces.md](docs/interfaces.md) for contract specifications, and
[CLAUDE.md](CLAUDE.md) for a codebase orientation aimed at AI coding agents.

## Project Structure

```text
api/               # FastAPI proxy layer connecting frontend to engine
frontend/          # React/Vite Web UI for simulation monitoring
sim/
├── core/          # Payment engine, accounts, settlements (Person 1)
├── population/    # Agent behaviour, calibration, profiles (Person 2)
├── chrono/        # Branching event store, checkpoints, replay (Person 3)
├── scheduler/     # Deterministic RNG, discrete-event env (Shared)
├── gateway/       # Tool registry, policy, capability checks (Person 1)
└── observability/ # Tracing, metrics, structured logging (Person 3)
```

## Contributing

Cross-subsystem imports must go through `sim/<subsystem>/interfaces.py`
only — concrete implementation modules (`engine.py`, `store.py`, etc.) are
off-limits to other subsystems and enforced by `make lint`'s import-linter
contracts. See [docs/interfaces.md](docs/interfaces.md) for the contracts
themselves.

## License

TBD
