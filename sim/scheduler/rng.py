"""DeterministicRNG using numpy.SeedSequence.

All randomness in the simulation MUST flow through DeterministicRNG instances.
Direct use of `random`, `numpy.random.seed()`, or global numpy random state
is strictly prohibited.

Key properties:
    - Spawned child streams are statistically independent.
    - Entity-keyed streams produce identical sequences across forks
      regardless of creation order (via deterministic key derivation).
    - State can be captured and restored for checkpointing.
"""
from __future__ import annotations

import hashlib
import pickle
from typing import TYPE_CHECKING

import numpy as np
from numpy.random import Generator, SeedSequence

if TYPE_CHECKING:
    from collections.abc import Sequence


class DeterministicRNG:
    """Deterministic, forkable random number generator.

    Uses numpy's SeedSequence for provably independent streams
    and Generator for high-quality random number generation.
    """

    __slots__ = ("_seed_seq", "_generator")

    def __init__(self, seed_seq: SeedSequence) -> None:
        self._seed_seq = seed_seq
        self._generator = Generator(np.random.PCG64(seed_seq))

    @classmethod
    def from_seed(cls, seed: int) -> DeterministicRNG:
        """Create a root RNG from an integer seed."""
        return cls(SeedSequence(seed))

    def spawn(self, *labels: str) -> list[DeterministicRNG]:
        """Create independent child streams, one per label.

        Each child is statistically independent from the parent
        and from each other.
        """
        children = self._seed_seq.spawn(len(labels))
        return [DeterministicRNG(child) for child in children]

    def spawn_for_entity(
        self, entity_type: str, entity_id: str
    ) -> DeterministicRNG:
        """Create an entity-keyed child stream.

        Uses deterministic key derivation (SHA-256 hash of entity_type + entity_id)
        so that the same entity receives identical RNG streams across forks,
        regardless of creation order.

        Args:
            entity_type: Type of entity (e.g., "user", "merchant")
            entity_id: Unique entity identifier (UUIDv7)

        Returns:
            A new DeterministicRNG with a deterministically derived seed.
        """
        # Derive a deterministic seed from the parent seed + entity key
        key = f"{entity_type}:{entity_id}".encode()
        parent_entropy = self._seed_seq.entropy
        # Combine parent entropy with entity key via SHA-256
        h = hashlib.sha256()
        # Handle both int and array entropy
        if isinstance(parent_entropy, int):
            h.update(parent_entropy.to_bytes(16, "big"))
        else:
            h.update(bytes(parent_entropy))  # type: ignore[arg-type]
        h.update(key)
        derived_seed = int.from_bytes(h.digest()[:16], "big")
        return DeterministicRNG(SeedSequence(derived_seed))

    # ── Sampling Methods ──────────────────────────────────────────────

    def random(self) -> float:
        """Uniform float in [0, 1)."""
        return float(self._generator.random())

    def uniform(self, low: float = 0.0, high: float = 1.0) -> float:
        """Uniform float in [low, high)."""
        return float(self._generator.uniform(low, high))

    def normal(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        """Sample from normal distribution."""
        return float(self._generator.normal(mu, sigma))

    def lognormal(self, mu: float = 0.0, sigma: float = 1.0) -> float:
        """Sample from log-normal distribution."""
        return float(self._generator.lognormal(mu, sigma))

    def poisson(self, lam: float = 1.0) -> int:
        """Sample from Poisson distribution."""
        return int(self._generator.poisson(lam))

    def exponential(self, scale: float = 1.0) -> float:
        """Sample from exponential distribution."""
        return float(self._generator.exponential(scale))

    def integers(self, low: int, high: int) -> int:
        """Random integer in [low, high)."""
        return int(self._generator.integers(low, high))

    def choice(
        self,
        seq: Sequence[object],
        p: Sequence[float] | None = None,
    ) -> object:
        """Choose a random element from a sequence.

        Args:
            seq: Sequence to choose from.
            p: Optional probability weights (must sum to 1.0).
        """
        idx = self._generator.choice(len(seq), p=p)
        return seq[int(idx)]

    def shuffle(self, seq: list[object]) -> None:
        """Shuffle a list in-place."""
        self._generator.shuffle(seq)  # type: ignore[arg-type]

    # ── State Management ──────────────────────────────────────────────

    def get_state(self) -> bytes:
        """Capture the current RNG state for checkpointing.

        Returns:
            Pickled state that can be restored via set_state().
        """
        bit_gen = self._generator.bit_generator
        state = {
            "seed_seq_entropy": self._seed_seq.entropy,
            "seed_seq_spawn_key": self._seed_seq.spawn_key,
            "seed_seq_n_children": self._seed_seq.n_children_spawned,
            "bit_generator_state": bit_gen.state,
        }
        return pickle.dumps(state)

    def set_state(self, state: bytes) -> None:
        """Restore RNG state from a checkpoint.

        Args:
            state: Bytes produced by get_state().
        """
        data = pickle.loads(state)  # noqa: S301
        self._generator.bit_generator.state = data["bit_generator_state"]
        # Reconstruct seed sequence with correct spawn tracking
        self._seed_seq = SeedSequence(
            entropy=data["seed_seq_entropy"],
            spawn_key=data["seed_seq_spawn_key"],
        )

    def __repr__(self) -> str:
        return f"DeterministicRNG(entropy={self._seed_seq.entropy!r})"
