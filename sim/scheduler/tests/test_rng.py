"""Unit tests for DeterministicRNG."""
from __future__ import annotations

import pytest

from sim.scheduler.rng import DeterministicRNG


class TestDeterministicRNGCreation:
    """Tests for RNG creation and basic properties."""

    def test_from_seed_creates_rng(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        assert isinstance(rng, DeterministicRNG)

    def test_same_seed_same_sequence(self) -> None:
        rng1 = DeterministicRNG.from_seed(42)
        rng2 = DeterministicRNG.from_seed(42)
        assert [rng1.random() for _ in range(100)] == [
            rng2.random() for _ in range(100)
        ]

    def test_different_seeds_different_sequences(self) -> None:
        rng1 = DeterministicRNG.from_seed(42)
        rng2 = DeterministicRNG.from_seed(43)
        seq1 = [rng1.random() for _ in range(100)]
        seq2 = [rng2.random() for _ in range(100)]
        assert seq1 != seq2


class TestSpawn:
    """Tests for stream spawning and independence."""

    def test_spawn_creates_correct_count(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        children = rng.spawn("a", "b", "c")
        assert len(children) == 3

    def test_spawned_streams_are_independent(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        child_a, child_b = rng.spawn("a", "b")
        seq_a = [child_a.random() for _ in range(100)]
        seq_b = [child_b.random() for _ in range(100)]
        assert seq_a != seq_b

    def test_spawn_is_deterministic(self) -> None:
        rng1 = DeterministicRNG.from_seed(42)
        rng2 = DeterministicRNG.from_seed(42)
        child1 = rng1.spawn("x")[0]
        child2 = rng2.spawn("x")[0]
        assert [child1.random() for _ in range(100)] == [
            child2.random() for _ in range(100)
        ]


class TestSpawnForEntity:
    """Tests for entity-keyed stream derivation."""

    def test_same_entity_same_stream(self) -> None:
        rng1 = DeterministicRNG.from_seed(42)
        rng2 = DeterministicRNG.from_seed(42)
        e1 = rng1.spawn_for_entity("user", "abc-123")
        e2 = rng2.spawn_for_entity("user", "abc-123")
        assert [e1.random() for _ in range(100)] == [
            e2.random() for _ in range(100)
        ]

    def test_different_entities_different_streams(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        e1 = rng.spawn_for_entity("user", "abc-123")
        e2 = rng.spawn_for_entity("user", "def-456")
        assert [e1.random() for _ in range(50)] != [
            e2.random() for _ in range(50)
        ]

    def test_entity_stream_independent_of_creation_order(self) -> None:
        """Entity streams must be identical regardless of what other entities
        were spawned before them (key property for fork determinism)."""
        rng1 = DeterministicRNG.from_seed(42)
        # Spawn user A first, then user B
        _ = rng1.spawn_for_entity("user", "aaa")
        e1_b = rng1.spawn_for_entity("user", "bbb")

        rng2 = DeterministicRNG.from_seed(42)
        # Spawn user B directly (no user A)
        e2_b = rng2.spawn_for_entity("user", "bbb")

        assert [e1_b.random() for _ in range(100)] == [
            e2_b.random() for _ in range(100)
        ]


class TestSamplingMethods:
    """Tests for sampling distribution methods."""

    def test_normal_produces_floats(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        val = rng.normal(0.0, 1.0)
        assert isinstance(val, float)

    def test_lognormal_positive(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        for _ in range(100):
            assert rng.lognormal(0.0, 1.0) > 0

    def test_poisson_non_negative(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        for _ in range(100):
            assert rng.poisson(5.0) >= 0

    def test_integers_in_range(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        for _ in range(100):
            val = rng.integers(10, 20)
            assert 10 <= val < 20

    def test_choice_from_sequence(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        options = ["a", "b", "c"]
        for _ in range(100):
            assert rng.choice(options) in options

    def test_choice_with_weights(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        options = ["rare", "common"]
        weights = [0.01, 0.99]
        results = [rng.choice(options, p=weights) for _ in range(1000)]
        # "common" should appear far more often
        assert results.count("common") > 900

    def test_exponential_positive(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        for _ in range(100):
            assert rng.exponential(1.0) > 0


class TestStateManagement:
    """Tests for state capture and restoration."""

    def test_get_set_state_restores_sequence(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        # Advance the RNG
        _ = [rng.random() for _ in range(50)]
        # Capture state
        state = rng.get_state()
        # Generate some values
        expected = [rng.random() for _ in range(100)]
        # Restore state
        rng.set_state(state)
        # Should produce identical values
        actual = [rng.random() for _ in range(100)]
        assert actual == expected

    def test_state_is_bytes(self) -> None:
        rng = DeterministicRNG.from_seed(42)
        state = rng.get_state()
        assert isinstance(state, bytes)

    def test_state_roundtrip_across_instances(self) -> None:
        rng1 = DeterministicRNG.from_seed(42)
        _ = [rng1.random() for _ in range(50)]
        state = rng1.get_state()

        # Create a new RNG from a different seed and restore state
        rng2 = DeterministicRNG.from_seed(99)
        rng2.set_state(state)

        # Both should produce identical sequences from this point
        assert [rng1.random() for _ in range(100)] == [
            rng2.random() for _ in range(100)
        ]
