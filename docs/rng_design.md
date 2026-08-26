# Deterministic RNG Design

## Overview

The `DeterministicRNG` class (`sim/scheduler/rng.py`) is the single randomness provider for FinSim.

### Invariant

> **All randomness in the simulation MUST flow through `DeterministicRNG` instances.**
> Direct calls to `random`, `numpy.random.seed()`, or global `numpy.random` state are strictly forbidden.

---

## Implementation Details

`DeterministicRNG` is built on NumPy's `SeedSequence` and `PCG64` bit generator:

```python
class DeterministicRNG:
    __slots__ = ("_seed_seq", "_generator")

    def __init__(self, seed_seq: SeedSequence) -> None:
        self._seed_seq = seed_seq
        self._generator = Generator(np.random.PCG64(seed_seq))
```

### 1. Root Initialization
```python
rng = DeterministicRNG.from_seed(42)
```

### 2. Stream Spawning
```python
# Creates independent child streams from parent seed sequence
core_rng, pop_rng = rng.spawn("core", "population")
```

### 3. Entity-Keyed Stream Derivation
To ensure entity streams are identical across simulation forks regardless of entity creation order:
```python
user_rng = pop_rng.spawn_for_entity("user", "018f3a5e-...")
```
Derivation logic:
$$\text{seed} = \text{int}(\text{SHA-256}(\text{parent\_entropy} \parallel \text{entity\_type} \parallel \text{":"} \parallel \text{entity\_id})[:16])$$

### 4. State Snapshotting & Restoration
For checkpointing in ChronoDAG:
```python
state_bytes = rng.get_state()
# Restore state
new_rng = DeterministicRNG.from_seed(0)
new_rng.set_state(state_bytes)
```

---

## Public Sampling API

- `random() -> float`: Uniform float in $[0, 1)$.
- `uniform(low=0.0, high=1.0) -> float`: Uniform float in $[\text{low}, \text{high})$.
- `normal(mu=0.0, sigma=1.0) -> float`: Gaussian distribution.
- `lognormal(mu=0.0, sigma=1.0) -> float`: Log-normal distribution.
- `poisson(lam=1.0) -> int`: Poisson count.
- `exponential(scale=1.0) -> float`: Exponential distribution.
- `integers(low: int, high: int) -> int`: Integer in $[\text{low}, \text{high})$.
- `choice(seq: Sequence, p: Sequence[float] | None = None) -> object`: Sample element.
- `shuffle(seq: list) -> None`: In-place shuffle.
