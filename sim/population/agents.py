"""Population manager and agent lifecycle orchestration for discrete-event simulation.

Handles batch initialization of user and merchant agent populations,
tracks active agent entities, and coordinates scheduling of behavioral
action loops in the discrete-event simulation queue.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sim.core.interfaces import (
    ActorRole,
    MerchantDirectoryEntry,
)
from sim.population.behaviour import PopulationBehaviourModel, _generate_deterministic_uuid
from sim.scheduler.rng import DeterministicRNG


@dataclass(frozen=True)
class AgentEntity:
    """Represents a simulated agent within the population subsystem."""
    entity_id: str
    role: ActorRole
    account_id: str
    initial_balance_paise: int
    kyc_level: int
    linked_device_ids: tuple[str, ...]
    merchant_category_code: str | None = None
    rating: float = 4.5
    settlement_rail: str = "UPI"


class PopulationManager:
    """Manages the lifecycle and action scheduling of the agent population."""

    def __init__(
        self,
        behaviour_model: PopulationBehaviourModel,
        root_rng: DeterministicRNG,
    ) -> None:
        self._behaviour_model = behaviour_model
        self._root_rng = root_rng
        self._users: list[AgentEntity] = []
        self._merchants: list[AgentEntity] = []

    @property
    def users(self) -> tuple[AgentEntity, ...]:
        return tuple(self._users)

    @property
    def merchants(self) -> tuple[AgentEntity, ...]:
        return tuple(self._merchants)

    @property
    def all_agents(self) -> tuple[AgentEntity, ...]:
        return tuple(self._users + self._merchants)

    def create_population(
        self, num_users: int = 50, num_merchants: int = 10
    ) -> tuple[AgentEntity, ...]:
        """Batch initialize user and merchant agents deterministically.

        Args:
            num_users: Number of retail user agents to spawn.
            num_merchants: Number of merchant agents to spawn.

        Returns:
            Tuple of all generated AgentEntity instances.
        """
        self._users.clear()
        self._merchants.clear()

        # 1. Create Merchants first (so users have merchants to transact with)
        for i in range(num_merchants):
            merchant_id = _generate_deterministic_uuid(self._root_rng)
            rng = self._root_rng.spawn_for_entity("merchant", merchant_id)
            init_data = self._behaviour_model.initialize_entity(
                merchant_id, "merchant", rng
            )

            device_id = _generate_deterministic_uuid(rng)
            init_bal = int(str(init_data.get("initial_balance_paise", 0)))
            init_kyc = int(str(init_data.get("kyc_level", 3)))
            merchant = AgentEntity(
                entity_id=merchant_id,
                role=ActorRole.MERCHANT,
                account_id=str(init_data["account_id"]),
                initial_balance_paise=init_bal,
                kyc_level=init_kyc,
                linked_device_ids=(device_id,),
                merchant_category_code=str(init_data.get("merchant_category_code", "5411")),
                rating=float(str(init_data.get("rating", 4.5))),
                settlement_rail=str(init_data.get("settlement_rail", "UPI")),
            )
            self._merchants.append(merchant)

        # 2. Create Users
        for i in range(num_users):
            user_id = _generate_deterministic_uuid(self._root_rng)
            rng = self._root_rng.spawn_for_entity("user", user_id)
            init_data = self._behaviour_model.initialize_entity(
                user_id, "user", rng
            )

            device_id = _generate_deterministic_uuid(rng)
            init_bal = int(str(init_data.get("initial_balance_paise", 0)))
            init_kyc = int(str(init_data.get("kyc_level", 0)))
            user = AgentEntity(
                entity_id=user_id,
                role=ActorRole.USER,
                account_id=str(init_data["account_id"]),
                initial_balance_paise=init_bal,
                kyc_level=init_kyc,
                linked_device_ids=(device_id,),
            )
            self._users.append(user)

        return self.all_agents

    def get_public_merchant_directory(self) -> tuple[MerchantDirectoryEntry, ...]:
        """Build public merchant directory entries visible to users."""
        return tuple(
            MerchantDirectoryEntry(
                merchant_id=m.entity_id,
                name=f"Merchant-{m.entity_id[:8]}",
                category=m.merchant_category_code or "5411",
                avg_rating=m.rating,
                settlement_rail=m.settlement_rail,
            )
            for m in self._merchants
        )

    def start_agent_loops(self, engine: Any) -> None:
        """Schedule the initial action loop for all user agents."""
        from sim.scheduler.env import ScheduledEvent
        from sim.core.interfaces import TransactionType

        for user in self._users:
            dt = self._behaviour_model.get_next_interarrival(
                user.entity_id, TransactionType.PAYMENT, engine.sim_time_ns
            )
            engine.schedule_event(ScheduledEvent(
                time_ns=engine.sim_time_ns + dt,
                handler=self._agent_step,
                payload={"engine": engine, "agent_id": user.entity_id, "role": user.role},
                description=f"Agent {user.entity_id} step"
            ))

    def _agent_step(self, engine: Any, agent_id: str, role: ActorRole) -> None:
        """Execute one step of an agent's behaviour and schedule the next."""
        from sim.core.interfaces import Command, TransactionType
        from sim.scheduler.env import ScheduledEvent
        import uuid

        # 1. Observe the world
        world_view = engine.get_world_view(actor_id=agent_id, actor_role=role)

        # 2. Propose actions (Intents)
        intents = self._behaviour_model.propose_actions(agent_id, world_view)

        # 3. Execute actions (convert Intent to Command)
        for intent in intents:
            cmd = Command(
                command_id=str(uuid.uuid4()),
                actor_id=intent.actor_id,
                action_type=intent.action_type,
                source_account_id=world_view.accounts[0].account_id if world_view.accounts else None,
                target_account_id=intent.target_id,
                amount_paise=intent.amount_paise,
                idempotency_key=intent.idempotency_key,
                device_id=intent.device_id,
                gateway_hint=intent.gateway_hint,
                metadata=intent.metadata,
            )
            # In a full run, we might use Gateway, but Engine is direct here
            engine.execute_command(cmd)

        # 4. Schedule next step
        dt = self._behaviour_model.get_next_interarrival(
            agent_id, TransactionType.PAYMENT, engine.sim_time_ns
        )
        engine.schedule_event(ScheduledEvent(
            time_ns=engine.sim_time_ns + dt,
            handler=self._agent_step,
            payload={"engine": engine, "agent_id": agent_id, "role": role},
            description=f"Agent {agent_id} step"
        ))
