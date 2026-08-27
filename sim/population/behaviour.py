"""BehaviourModel implementation for retail population simulation.

Implements the BehaviourModel protocol:
  - Proposes action Intents based on actor WorldView and balance-spring dynamics
  - Initializes entity attributes (accounts, KYC, devices, MCC)
  - Schedules discrete-event inter-arrival times using 24x7 temporal matrices
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sim.core.interfaces import (
    AccountStatus,
    AccountType,
    ActorRole,
    DeviceType,
    TransactionType,
    WorldView,
)
from sim.observability import traced
from sim.population.interfaces import (
    BehaviourModel,
    CalibratedParams,
    Intent,
)
from sim.population.profiles import ProfileSampler
from sim.population.temporal import TemporalModel
from sim.scheduler.rng import DeterministicRNG


# Canonical entity-RNG keying scheme: "user"/"merchant" (lowercase), matching
# PopulationManager.create_population's spawn_for_entity() calls. Every RNG
# consumer for a given entity MUST route through get_entity_rng() with these
# same keys so the entity gets one continuous, cached stream instead of a
# fresh (and previously inconsistently-keyed) spawn per call site.
_ROLE_ENTITY_TYPE: dict[ActorRole, str] = {
    ActorRole.USER: "user",
    ActorRole.MERCHANT: "merchant",
}


def _generate_deterministic_uuid(rng: DeterministicRNG) -> str:
    """Generate a deterministic UUID string using random bytes from DeterministicRNG."""
    # Generate 16 bytes from RNG
    rand_ints = [int(rng.integers(0, 256)) for _ in range(16)]
    raw_bytes = bytearray(rand_ints)
    # Set UUID version 7
    raw_bytes[6] = (raw_bytes[6] & 0x0F) | 0x70  # Version 7
    raw_bytes[8] = (raw_bytes[8] & 0x3F) | 0x80  # Variant RFC 4122
    return str(uuid.UUID(bytes=bytes(raw_bytes)))


class PopulationBehaviourModel:
    """Standard population behaviour model implementing the BehaviourModel protocol."""

    def __init__(
        self,
        params: CalibratedParams,
        root_rng: DeterministicRNG | None = None,
    ) -> None:
        self._params = params
        self._root_rng = root_rng or DeterministicRNG.from_seed(42)
        self._profile_sampler = ProfileSampler(params)
        self._temporal_model = TemporalModel(params)
        self._entity_rng_cache: dict[str, DeterministicRNG] = {}

    @property
    def params(self) -> CalibratedParams:
        return self._params

    def get_entity_rng(
        self, entity_id: str, entity_type: str = "actor"
    ) -> DeterministicRNG:
        """Get or derive a deterministic child RNG for a specific entity."""
        key = f"{entity_type}:{entity_id}"
        if key not in self._entity_rng_cache:
            self._entity_rng_cache[key] = self._root_rng.spawn_for_entity(
                entity_type, entity_id
            )
        return self._entity_rng_cache[key]

    def initialize_entity(
        self, entity_id: str, entity_type: str, rng: DeterministicRNG
    ) -> dict[str, object]:
        """Generate initial attributes for a new entity.

        Args:
            entity_id: UUIDv7 of the entity.
            entity_type: "user", "merchant", etc.
            rng: Entity's DeterministicRNG.

        Returns:
            Dict containing initial entity attributes.
        """
        norm_type = entity_type.lower()
        if "merchant" in norm_type or "business" in norm_type:
            # Merchant initialization
            base_balance = self._profile_sampler.sample_initial_balance(rng) * 5
            mcc = self._profile_sampler.sample_merchant_category(rng)
            rating = round(rng.uniform(3.5, 5.0), 1)
            settlement_rail = str(rng.choice(["UPI", "IMPS", "NEFT"]))

            return {
                "entity_id": entity_id,
                "account_id": _generate_deterministic_uuid(rng),
                "account_type": AccountType.MERCHANT,
                "initial_balance_paise": base_balance,
                "kyc_level": 3,
                "merchant_category_code": mcc,
                "rating": rating,
                "settlement_rail": settlement_rail,
                "device_type": DeviceType.POS,
            }

        # User initialization (Retail client)
        initial_balance = self._profile_sampler.sample_initial_balance(rng)
        # KYC Level assignment based on initial wealth
        if initial_balance >= 5_000_000:    # >= ₹50,000
            kyc_level = 3
        elif initial_balance >= 1_000_000:  # >= ₹10,000
            kyc_level = 2
        elif initial_balance >= 100_000:    # >= ₹1,000
            kyc_level = 1
        else:
            kyc_level = 0

        # Device selection: 80% Mobile, 20% Browser
        device_type = DeviceType.MOBILE if rng.random() < 0.80 else DeviceType.BROWSER

        return {
            "entity_id": entity_id,
            "account_id": _generate_deterministic_uuid(rng),
            "account_type": AccountType.PERSONAL,
            "initial_balance_paise": initial_balance,
            "kyc_level": kyc_level,
            "device_type": device_type,
        }

    @traced("BehaviourModel.propose_actions")
    def propose_actions(
        self, entity_id: str, world_view: WorldView
    ) -> list[Intent]:
        """Generate a list of Intents for the given entity based on visible WorldView.

        Applies the balance-spring dynamic:
          - High balance -> higher outflow probabilities (PAYMENT, TRANSFER, CASH_OUT)
          - Low balance -> suppressed outflows, higher inflow probabilities (CASH_IN, DEBIT)
        """
        rng = self.get_entity_rng(
            entity_id, _ROLE_ENTITY_TYPE.get(world_view.actor_role, "actor")
        )

        # Check if actor has active accounts
        active_accounts = [
            acc for acc in world_view.accounts if acc.status == AccountStatus.ACTIVE
        ]
        if not active_accounts:
            return []

        total_balance_paise = sum(acc.balance_paise for acc in active_accounts)

        # ── User Actor Logic ──────────────────────────────────────────
        if world_view.actor_role == ActorRole.USER:
            return self._propose_user_actions(
                entity_id, world_view, total_balance_paise, rng
            )

        # ── Merchant Actor Logic ──────────────────────────────────────
        if world_view.actor_role == ActorRole.MERCHANT:
            return self._propose_merchant_actions(
                entity_id, world_view, total_balance_paise, rng
            )

        return []

    def _propose_user_actions(
        self,
        entity_id: str,
        world_view: WorldView,
        total_balance_paise: int,
        rng: DeterministicRNG,
    ) -> list[Intent]:
        """Propose actions for a retail user applying the balance-spring dynamic."""
        # Baseline reference balance: ₹1,000 (100,000 paise)
        baseline_paise = 100_000
        spring_ratio = max(0.05, total_balance_paise / baseline_paise)

        # Compute dynamic action type probabilities
        action_weights: dict[TransactionType, float] = {}

        if spring_ratio >= 1.0:
            # High balance: Outflow actions dominate
            action_weights[TransactionType.PAYMENT] = 0.50 * min(2.0, spring_ratio)
            action_weights[TransactionType.TRANSFER] = 0.30 * min(2.0, spring_ratio)
            action_weights[TransactionType.CASH_OUT] = 0.15 * min(1.5, spring_ratio)
            action_weights[TransactionType.CASH_IN] = 0.03 / spring_ratio
            action_weights[TransactionType.DEBIT] = 0.02 / spring_ratio
        else:
            # Low balance: Inflow actions dominate or pause outflows
            action_weights[TransactionType.CASH_IN] = 0.45 / spring_ratio
            action_weights[TransactionType.DEBIT] = 0.35 / spring_ratio
            action_weights[TransactionType.PAYMENT] = 0.15 * spring_ratio
            action_weights[TransactionType.TRANSFER] = 0.05 * spring_ratio
            action_weights[TransactionType.CASH_OUT] = 0.0

        # Filter by available calibrated profiles
        valid_types = [
            t for t in action_weights if t in self._params.profiles_by_type
        ]
        if not valid_types:
            return []

        weights = [action_weights[t] for t in valid_types]
        total_w = sum(weights)
        if total_w <= 0:
            return []

        norm_probs = [w / total_w for w in weights]
        indices = list(range(len(valid_types)))
        chosen_val = rng.choice(indices, p=norm_probs)
        chosen_idx = int(str(chosen_val))
        chosen_type = valid_types[chosen_idx]

        # Sample profile, repetitions, and amount
        profile = self._profile_sampler.sample_profile(chosen_type, rng)
        rep_count = self._profile_sampler.sample_repetition_count(profile, rng)

        device_id = (
            world_view.devices[0].device_id if world_view.devices else None
        )

        intents: list[Intent] = []
        for _ in range(rep_count):
            amount_paise = self._profile_sampler.sample_amount_paise(profile, rng)

            # For outflows, clamp to available balance
            if chosen_type in (
                TransactionType.PAYMENT,
                TransactionType.TRANSFER,
                TransactionType.CASH_OUT,
            ):
                if total_balance_paise < 100:
                    break
                amount_paise = min(amount_paise, total_balance_paise)

            # Target resolution
            target_id: str | None = None
            if chosen_type == TransactionType.PAYMENT:
                if world_view.merchants:
                    # Sample merchant weighted by rating
                    ratings = [max(1.0, m.avg_rating) for m in world_view.merchants]
                    total_r = sum(ratings)
                    p_ratings = [r / total_r for r in ratings]
                    chosen_m = rng.choice(world_view.merchants, p=p_ratings)
                    target_id = getattr(chosen_m, "merchant_id", None)
            elif chosen_type == TransactionType.TRANSFER:
                target_id = _generate_deterministic_uuid(rng)

            idempotency_key = _generate_deterministic_uuid(rng)

            intents.append(
                Intent(
                    actor_id=entity_id,
                    action_type=chosen_type,
                    target_id=target_id,
                    amount_paise=amount_paise,
                    idempotency_key=idempotency_key,
                    device_id=device_id,
                )
            )

        return intents

    def _propose_merchant_actions(
        self,
        entity_id: str,
        world_view: WorldView,
        total_balance_paise: int,
        rng: DeterministicRNG,
    ) -> list[Intent]:
        """Propose passive merchant actions (low-probability refund or settlement sweep),
        gated and weighted by the calibrated 24x7 temporal rates rather than a fixed split."""
        # Merchants have low probability of initiating unprompted actions
        if total_balance_paise < 10_000:
            return []

        refund_rate = self._temporal_model.get_rate(TransactionType.REFUND, world_view.sim_time_ns)
        settlement_rate = self._temporal_model.get_rate(TransactionType.SETTLEMENT, world_view.sim_time_ns)
        combined_rate = refund_rate + settlement_rate  # actions/hour, per calibrated PaySim data

        # Convert the combined hourly rate into a per-call action probability.
        # Calibrated PaySim rates are typically well under 100/hr; cap at 0.5
        # so merchants stay passive/low-probability per the plan's intent.
        action_probability = min(0.5, combined_rate / 100.0)
        if rng.random() > action_probability:
            return []

        p_refund = refund_rate / combined_rate if combined_rate > 0 else 0.5
        action_type = TransactionType.REFUND if rng.random() < p_refund else TransactionType.SETTLEMENT
        amount_paise = int(round(rng.uniform(500, min(50_000, total_balance_paise))))
        idempotency_key = _generate_deterministic_uuid(rng)
        device_id = world_view.devices[0].device_id if world_view.devices else None

        return [
            Intent(
                actor_id=entity_id,
                action_type=action_type,
                target_id=_generate_deterministic_uuid(rng),
                amount_paise=amount_paise,
                idempotency_key=idempotency_key,
                device_id=device_id,
            )
        ]

    def get_next_interarrival(
        self, entity_id: str, action_type: TransactionType, current_time_ns: float, population_size: int = 1
    ) -> float:
        """Sample the next inter-arrival delay in nanoseconds."""
        rng = self.get_entity_rng(entity_id)
        return self._temporal_model.sample_next_interarrival(
            action_type, current_time_ns, rng, population_size
        )
