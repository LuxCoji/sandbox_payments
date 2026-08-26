"""Population subsystem contracts.

Defines the Intent (output of BehaviourModel), ActionProfile,
CalibratedParams, and the BehaviourModel protocol.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sim.core.interfaces import TransactionType, WorldView

if TYPE_CHECKING:
    from sim.scheduler.rng import DeterministicRNG


@dataclass(frozen=True)
class Intent:
    """An action proposed by the BehaviourModel for execution.

    The WorldEngine converts Intents into Commands after validation.
    """
    actor_id: str                            # UUIDv7 of the acting entity
    action_type: TransactionType
    target_id: str | None                    # Destination account or merchant
    amount_paise: int                        # INR paise
    idempotency_key: str                     # UUIDv7, prevents duplicate execution
    device_id: str | None = None             # Optional: device used (for red-team control)
    gateway_hint: str | None = None          # Optional: preferred gateway (for red-team control)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionProfile:
    """Calibrated parameters for a single action type.

    Derived from PaySim clientsProfiles.csv.
    """
    action_type: TransactionType
    min_count: int                           # Min repetitions per step
    max_count: int                           # Max repetitions per step
    avg_amount_paise: int
    std_amount_paise: int
    frequency: float                         # Actions per sim-day


@dataclass(frozen=True)
class CalibratedParams:
    """Complete set of calibrated population parameters.

    Loaded from calibrated_params.json, produced by scripts/calibrate.py.
    """
    profiles_by_type: dict[TransactionType, tuple[ActionProfile, ...]]
    initial_balance_distribution: tuple[tuple[int, int, float], ...]  # (min, max, weight)
    max_occurrences_per_client: dict[TransactionType, int]
    temporal_rate_matrix: dict[TransactionType, tuple[tuple[float, ...], ...]]  # 24×7 rates
    merchant_category_distribution: dict[str, float]  # MCC → probability


@runtime_checkable
class BehaviourModel(Protocol):
    """Protocol for the population behaviour engine.

    The BehaviourModel decides what each entity does at each step,
    based on the WorldView (their visible state) and calibrated parameters.
    """

    def propose_actions(
        self, entity_id: str, world_view: WorldView
    ) -> list[Intent]:
        """Generate a list of Intents for the given entity.

        USER entities:
            - Balance-spring dynamic: higher balances → more outflows
            - Sample action type → profile → repetition count → log-normal amount
            - Select targets (merchant affinity for PAYMENT, peers for TRANSFER)

        MERCHANT entities:
            - Low-probability passive actions (REFUND, PAYOUT)
            - Based on balance and temporal rates

        Returns:
            List of Intent objects to be processed by the WorldEngine.
        """
        ...

    def initialize_entity(
        self, entity_id: str, entity_type: str, rng: DeterministicRNG
    ) -> dict[str, object]:
        """Generate initial attributes for a new entity.

        Returns:
            Dict with keys: initial_balance_paise, kyc_level,
            merchant_category_code (if merchant), device_type, etc.
        """
        ...

    def get_next_interarrival(
        self, entity_id: str, action_type: TransactionType, current_time_ns: float
    ) -> float:
        """Sample the next inter-arrival time for scheduling.

        Uses the 24×7 temporal rate matrix to model time-of-day
        and day-of-week variation.

        Returns:
            Delta time in nanoseconds until next action.
        """
        ...
