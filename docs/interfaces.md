# Interface Contracts

This document is the source of truth for all subsystem contracts.

## Contract Rules

1. Only signatures, dataclass fields, and protocol methods appear in interface modules.
2. Cross-subsystem imports must target `sim.<subsystem>.interfaces` only.
3. Modifications must remain backward-compatible unless coordinated.
4. All contract changes require updating the corresponding contract test suite.

## Core Interfaces (`sim/core/interfaces.py`)

<!-- TODO: Document WorldEngine protocol, WorldView, AccountSnapshot -->

## Population Interfaces (`sim/population/interfaces.py`)

<!-- TODO: Document BehaviourModel protocol, CalibratedParams, Intent -->

## Chrono Interfaces (`sim/chrono/interfaces.py`)

<!-- TODO: Document ChronoDAG protocol, Checkpoint, Branch, StateDiff -->

## Gateway Interfaces (`sim/gateway/interfaces.py`)

<!-- TODO: Document ToolGateway protocol, ToolSpec, ActorContext -->

## Scheduler Interfaces (`sim/scheduler/rng.py`)

<!-- TODO: Document DeterministicRNG public API -->
