# Deterministic RNG Design

## Overview

<!-- TODO: Document RNG architecture using numpy.SeedSequence -->

## Seed Derivation Strategy

<!-- TODO: Document entity-keyed seed derivation -->

## Stream Independence

<!-- TODO: Document spawn semantics and independence guarantees -->

## State Capture & Restoration

<!-- TODO: Document get_state/set_state for checkpointing -->

## Forbidden Patterns

- No `import random` anywhere in the codebase
- No `numpy.random.seed()` or global `numpy.random` calls
- All randomness must flow through `DeterministicRNG` instances
