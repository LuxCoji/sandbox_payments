"""Composition root — wires all subsystems."""
from __future__ import annotations

import argparse

from sim.config import SimConfig
from sim.core.engine import WorldEngineImpl
from sim.gateway.gateway import ToolGatewayImpl
from sim.gateway.policy import RateLimiter
from sim.gateway.registry import ToolRegistry
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG


class NullChronoDAG:
    """In-memory ChronoDAG stub for MVP."""
    def __init__(self) -> None:
        self.events: list = []

    def save_event(self, event: object) -> None:
        self.events.append(event)

    def fork(self, *a: object, **kw: object) -> None: raise NotImplementedError
    def checkout(self, *a: object, **kw: object) -> None: raise NotImplementedError
    def replay(self, *a: object, **kw: object) -> None: raise NotImplementedError
    def diff(self, *a: object, **kw: object) -> None: raise NotImplementedError
    def create_checkpoint(self, *a: object, **kw: object) -> None: raise NotImplementedError
    def get_state_hash(self, *a: object, **kw: object) -> None: raise NotImplementedError


from sim.chrono.store import PostgresChronoDAG

def build_simulation(config: SimConfig) -> tuple[WorldEngineImpl, ToolGatewayImpl, PostgresChronoDAG]:
    rng = DeterministicRNG.from_seed(config.seed)
    env = SimulationEnv()
    engine = WorldEngineImpl(env=env, rng=rng)
    registry = ToolRegistry()
    rate_limiter = RateLimiter()
    gateway = ToolGatewayImpl(registry=registry, rate_limiter=rate_limiter, engine=engine)
    
    # Initialize the real DAG
    chrono = PostgresChronoDAG(config.db_url)
    
    _register_core_tools(registry, engine)

    return engine, gateway, chrono

def _register_core_tools(registry: ToolRegistry, engine: WorldEngineImpl) -> None:
    from sim.gateway.interfaces import ToolSpec, Capability
    from sim.core.interfaces import Command, TransactionType
    import uuid

    # 1. create_account
    def create_account_handler(context, params, engine):
        return []
    
    registry.register_tool(
        ToolSpec("create_account", "Create a new account", (), frozenset()),
        create_account_handler
    )

    # 2. transfer_funds
    def transfer_funds_handler(context, params, engine):
        cmd = Command(
            command_id=str(uuid.uuid4()),
            actor_id=context.actor_id,
            action_type=TransactionType.TRANSFER,
            source_account_id=str(params.get("source_account_id")),
            target_account_id=str(params.get("target_account_id")),
            amount_paise=int(str(params.get("amount_paise"))),
            idempotency_key=str(params.get("idempotency_key", uuid.uuid4()))
        )
        return engine.execute_command(cmd).events
        
    registry.register_tool(
        ToolSpec("transfer_funds", "Transfer funds", (Capability.TRANSFER_FUNDS,), frozenset({"events"})),
        transfer_funds_handler
    )

    # 3. make_payment
    def make_payment_handler(context, params, engine):
        cmd = Command(
            command_id=str(uuid.uuid4()),
            actor_id=context.actor_id,
            action_type=TransactionType.PAYMENT,
            source_account_id=str(params.get("source_account_id")),
            target_account_id=str(params.get("target_account_id")),
            amount_paise=int(str(params.get("amount_paise"))),
            idempotency_key=str(params.get("idempotency_key", uuid.uuid4())),
            gateway_hint=str(params.get("gateway_id", ""))
        )
        return engine.execute_command(cmd).events

    registry.register_tool(
        ToolSpec("make_payment", "Make a payment", (Capability.MAKE_PAYMENT,), frozenset({"events"})),
        make_payment_handler
    )

    # 4. inspect_account
    def inspect_account_handler(context, params, engine):
        view = engine.get_world_view(context.actor_id, context.actor_role)
        acc_id = params.get("account_id")
        for acc in view.accounts:
            if acc.account_id == acc_id:
                return [acc]
        return []
        
    registry.register_tool(
        ToolSpec("inspect_account", "Inspect account details", (Capability.VIEW_OWN_ACCOUNT,), frozenset({"events"})),
        inspect_account_handler
    )

def main() -> None:
    pass

if __name__ == "__main__":
    main()
