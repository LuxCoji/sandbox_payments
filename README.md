# FinSim – Financial Digital Twin + Adversarial Testing Environment

A deterministic, event-sourced financial simulation engine with branching time-travel, adversarial agent injection, and full observability.

## Overview

FinSim models a realistic retail payment ecosystem using discrete-event simulation, calibrated from real-world transaction data (PaySim). It supports:

- **Deterministic replay** – identical seeds produce identical state hashes
- **Branching (ChronoDAG)** – fork simulation state at any point, inject adversarial agents, and diff outcomes
- **Tool Gateway** – capability-gated, rate-limited API for agent interaction
- **Full observability** – OpenTelemetry traces, Prometheus metrics, structured logging

## Quick Start

```bash
# Install dependencies
uv sync --dev

# Run tests
make test

# Start local services (PostgreSQL, Jaeger, Prometheus, Grafana)
make up

# Run a simulation
make run

# Run regression tests
make regress
```

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full system design.

## Project Structure

```
sim/
├── core/          # Payment engine, accounts, settlements (Person 1)
├── population/    # Agent behaviour, calibration, profiles (Person 2)
├── chrono/        # Branching event store, checkpoints, replay (Person 3)
├── scheduler/     # Deterministic RNG, discrete-event env (Shared)
├── gateway/       # Tool registry, policy, capability checks (Person 1)
└── observability/ # Tracing, metrics, structured logging (Person 3)
```

## Contributing

See [docs/interfaces.md](docs/interfaces.md) for contract specifications.

## License

TBD
