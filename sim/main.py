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


def build_simulation(config: SimConfig) -> tuple[WorldEngineImpl, ToolGatewayImpl, NullChronoDAG]:
    rng = DeterministicRNG.from_seed(config.seed)
    env = SimulationEnv()
    engine = WorldEngineImpl(env=env, rng=rng)
    registry = ToolRegistry()
    rate_limiter = RateLimiter()
    gateway = ToolGatewayImpl(registry=registry, rate_limiter=rate_limiter, engine=engine)
    chrono = NullChronoDAG()

    _register_core_tools(registry, engine)

    return engine, gateway, chrono


def _register_core_tools(registry: ToolRegistry, engine: WorldEngineImpl) -> None:
    pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="finsim")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run-seed")
    run_parser.add_argument("--seed", type=int, default=42)
    run_parser.add_argument("--users", type=int, default=1000)
    run_parser.add_argument("--duration-days", type=int, default=30)

    args = parser.parse_args()
    if args.command == "run-seed":
        config = SimConfig(seed=args.seed, num_users=args.users, sim_duration_days=args.duration_days)
        engine, gateway, chrono = build_simulation(config)
        print(f"Simulation built. Initial hash: {engine.get_state_hash()}")

if __name__ == "__main__":
    main()
