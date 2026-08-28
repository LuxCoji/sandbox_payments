"""Composition root — wires all subsystems and exposes the FinSim CLI.

CLI entrypoint (per the original plan): ``python -m sim.main <command>`` with
subcommands ``run-seed``, ``fork-branch``, ``replay-branch``, ``diff-branches``.
"""
from __future__ import annotations

import argparse
import sys

from sim.chrono.store import PostgresChronoDAG
from sim.config import SimConfig
from sim.core.engine import WorldEngineImpl
from sim.gateway.gateway import ToolGatewayImpl
from sim.gateway.policy import RateLimiter
from sim.gateway.registry import ToolRegistry
from sim.observability import get_logger, setup_tracing, start_metrics_server
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG

logger = get_logger("finsim.cli")


def _require_db_url(config: SimConfig) -> str:
    if not config.db_url:
        raise SystemExit("No database URL: set FINSIM_DB_URL in the environment/.env or pass db_url on SimConfig")
    return config.db_url


def build_simulation(config: SimConfig) -> tuple[WorldEngineImpl, ToolGatewayImpl, PostgresChronoDAG]:
    rng = DeterministicRNG.from_seed(config.seed)
    env = SimulationEnv()

    # Chrono is wired into the engine so execute_command() persists every
    # emitted event via the Emit -> Append -> Apply pipeline (see engine.py).
    chrono = PostgresChronoDAG(_require_db_url(config))
    engine = WorldEngineImpl(env=env, rng=rng, chrono=chrono)

    registry = ToolRegistry()
    rate_limiter = RateLimiter()
    gateway = ToolGatewayImpl(registry=registry, rate_limiter=rate_limiter, engine=engine)

    _register_core_tools(registry, engine)

    return engine, gateway, chrono


def build_simulation_for_branch(
    config: SimConfig, branch_id: str
) -> tuple[WorldEngineImpl, ToolGatewayImpl, PostgresChronoDAG]:
    """Reconstruct a live simulation rooted at branch_id's latest checkpoint.

    Unlike build_simulation() (always fresh, always "main"), this restores
    engine state from a ChronoDAG checkpoint — the mechanism a red-team
    session uses to run on a forked branch. See
    docs/redteam_agent_design.md §3/§6 Phase 3.

    Known limitations (documented, not silently papered over):
      - Only supports checkout()ing a branch whose latest checkpoint is
        exactly at the branch head (replay_ctx.pending_events is empty).
        Nothing in this codebase deserializes StoredEvent.payload dicts back
        into live DomainEvent instances to replay into a rebuilt engine —
        that's a separate, unbuilt capability. The intended v1 usage
        (checkpoint immediately before fork, fork immediately from that
        checkpoint) never produces pending events, so this raises loudly
        instead of silently reconstructing stale/wrong state.
      - The scheduler's pending-event queue (SimulationEnv._queue) is not
        part of the checkpoint and is NOT restored — the rebuilt engine
        starts with an empty queue, no organic background traffic "in
        flight". See docs/redteam_agent_design.md "Known limitations".
      - Requires the checkpoint's aggregate_snapshot to have been produced
        by WorldEngineImpl.get_full_snapshot_bytes(), not
        get_canonical_state_bytes() (the latter is lossy, hash-only — see
        that method's docstring). Checkpoints created by api/sim_session.py
        use the lossy form for its own live in-process forking and are NOT
        restorable through this function.
    """
    chrono = PostgresChronoDAG(_require_db_url(config))
    replay_ctx = chrono.checkout(branch_id)

    if replay_ctx.pending_events:
        raise NotImplementedError(
            f"branch {branch_id!r} has {len(replay_ctx.pending_events)} event(s) "
            "after its latest checkpoint. build_simulation_for_branch() only "
            "supports checkpoint-at-head reconstruction — replaying "
            "StoredEvents back into a rebuilt engine isn't implemented. "
            "Create a fresh checkpoint at the branch's current head before "
            "calling this."
        )

    # The seed passed to from_seed() here is a throwaway placeholder —
    # set_state() immediately overwrites the seed sequence and generator
    # state wholesale with the checkpoint's actual RNG state.
    rng = DeterministicRNG.from_seed(0)
    rng.set_state(replay_ctx.checkpoint.rng_state)

    env = SimulationEnv(start_time_ns=int(replay_ctx.checkpoint.sim_time_ns))

    engine = WorldEngineImpl(
        env=env, rng=rng, branch_id=branch_id, chrono=chrono,
        seq_num=replay_ctx.checkpoint.event_number,
    )
    engine.restore_full_snapshot_bytes(replay_ctx.checkpoint.aggregate_snapshot)

    registry = ToolRegistry()
    rate_limiter = RateLimiter()
    gateway = ToolGatewayImpl(registry=registry, rate_limiter=rate_limiter, engine=engine)

    _register_core_tools(registry, engine)
    _register_redteam_tools(registry, engine, chrono)

    return engine, gateway, chrono


def _register_core_tools(registry: ToolRegistry, engine: WorldEngineImpl) -> None:
    import uuid

    from sim.core.interfaces import Command, TransactionType
    from sim.gateway.interfaces import Capability, ToolSpec

    # 1. create_account
    def create_account_handler(context, params, engine):
        return []

    registry.register_tool(
        ToolSpec("create_account", "Create a new account", frozenset(), {}),
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
        ToolSpec("transfer_funds", "Transfer funds", frozenset({Capability.TRANSFER_FUNDS}), {}),
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
        ToolSpec("make_payment", "Make a payment", frozenset({Capability.MAKE_PAYMENT}), {}),
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
        ToolSpec("inspect_account", "Inspect account details", frozenset({Capability.VIEW_OWN_ACCOUNT}), {}),
        inspect_account_handler
    )


def _register_redteam_tools(registry: ToolRegistry, engine: WorldEngineImpl, chrono: PostgresChronoDAG) -> None:
    """Tools only meaningful on a red-team session's branch — not registered
    by build_simulation() (main), only by build_simulation_for_branch().
    See docs/redteam_agent_design.md §3.
    """
    from sim.gateway.interfaces import Capability, ToolSpec

    def commit_strategy_handler(context, params, engine):
        branch = chrono.checkout(context.branch_id).branch
        chrono.update_branch_metadata(context.branch_id, {**branch.metadata, "origin": "committed"})
        return []

    registry.register_tool(
        ToolSpec(
            "commit_strategy",
            "Tag the current branch as the red-team agent's committed strategy "
            "(vs. a throwaway exploratory attempt)",
            frozenset({Capability.FORK_BRANCH}),
            {},
            rate_limit_tier="branch_op",
        ),
        commit_strategy_handler,
    )

def _resolve_db_url(args: argparse.Namespace) -> str:
    """CLI --db-url flag takes precedence; otherwise fall back to
    FINSIM_DB_URL from the environment/.env via SimConfig."""
    db_url = args.db_url or SimConfig().db_url
    if not db_url:
        raise SystemExit(
            "No database URL: pass --db-url or set FINSIM_DB_URL in the environment/.env"
        )
    return db_url


def run_seed(args: argparse.Namespace) -> None:
    """Run a new simulation from a deterministic seed."""
    import os

    from sim.population.agents import PopulationManager
    from sim.population.behaviour import PopulationBehaviourModel
    from sim.population.calibration import calibrate_from_csv

    logger.info("Initializing simulation...", seed=args.seed, users=args.users)

    config = SimConfig(
        seed=args.seed,
        num_users=args.users,
        sim_duration_days=max(1, int(args.duration_hours / 24)),
        db_url=args.db_url or SimConfig().db_url,
    )
    engine, _gateway, _dag = build_simulation(config)

    data_dir = os.path.join(os.path.dirname(__file__), "..", "data", "paysim")
    try:
        params = calibrate_from_csv(data_dir)
    except FileNotFoundError:
        from sim.population.interfaces import CalibratedParams
        params = CalibratedParams({}, (), {}, {}, {})

    behaviour_model = PopulationBehaviourModel(params, engine._rng)
    population = PopulationManager(behaviour_model, engine._rng)

    population.create_population(
        num_users=args.users, num_merchants=max(1, args.users // 10), engine=engine
    )
    population.start_agent_loops(engine)

    engine._env.run(until=args.duration_hours * 3600 * 1e9)

    logger.info(
        "Simulation finished",
        final_hash=engine.get_state_hash(),
        step_count=engine._env.step_count,
    )


def fork_branch(args: argparse.Namespace) -> None:
    """Create a new alternate-timeline branch from a checkpoint."""
    dag = PostgresChronoDAG(_resolve_db_url(args))
    branch = dag.fork(checkpoint_id=args.checkpoint, branch_id=args.branch)
    logger.info("Branch forked successfully", branch=args.branch, seed_offset=branch.seed_offset)


def replay_branch(args: argparse.Namespace) -> None:
    """Replay a branch's event log over a given event-number range."""
    dag = PostgresChronoDAG(_resolve_db_url(args))
    events = dag.replay(args.branch, args.from_event, args.to_event)
    logger.info("Replay complete", branch=args.branch, event_count=len(events))
    for event in events:
        logger.info(
            "event",
            event_id=event.event_id,
            event_type=event.event_type,
            seq_num=event.seq_num,
        )


def diff_branches(args: argparse.Namespace) -> None:
    """Compute the state diff between two branches at a specific event."""
    dag = PostgresChronoDAG(_resolve_db_url(args))
    try:
        diff = dag.diff(args.branch_a, args.branch_b, args.event)
        logger.info(
            "Diff computed",
            added=len(diff.entities_added),
            removed=len(diff.entities_removed),
            modified=len(diff.entities_modified),
            unique_events_a=diff.events_only_in_a,
            unique_events_b=diff.events_only_in_b,
        )
    except Exception as e:
        logger.error("Failed to compute diff", error=str(e))
        sys.exit(1)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FinSim CLI (Adversarial Sandbox)")
    parser.add_argument("--db-url", type=str, default=None, help="PostgreSQL URL (overrides FINSIM_DB_URL)")
    parser.add_argument("--metrics-port", type=int, default=8000, help="Port for Prometheus metrics")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_run = subparsers.add_parser("run-seed", help="Run a new simulation from a deterministic seed")
    p_run.add_argument("--seed", type=int, required=True, help="Deterministic RNG seed")
    p_run.add_argument("--users", type=int, default=1000, help="Number of users to generate")
    p_run.add_argument("--duration-hours", type=int, default=24, help="Simulation duration in hours")

    p_fork = subparsers.add_parser("fork-branch", help="Fork a new alternate timeline branch from a checkpoint")
    p_fork.add_argument("--checkpoint", type=str, required=True, help="Checkpoint ID to fork from")
    p_fork.add_argument("--branch", type=str, required=True, help="Name of the new branch")

    p_replay = subparsers.add_parser("replay-branch", help="Replay a branch's event log over an event range")
    p_replay.add_argument("--branch", type=str, required=True)
    p_replay.add_argument("--from-event", type=int, default=0, dest="from_event")
    p_replay.add_argument("--to-event", type=int, required=True, dest="to_event")

    p_diff = subparsers.add_parser("diff-branches", help="Compare states between two branches at a specific event")
    p_diff.add_argument("--branch-a", type=str, required=True, dest="branch_a")
    p_diff.add_argument("--branch-b", type=str, required=True, dest="branch_b")
    p_diff.add_argument("--event", type=int, required=True, help="Event sequence number to diff at")

    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    setup_tracing("finsim.runner")
    start_metrics_server(args.metrics_port)
    logger.info("Observability started", metrics_port=args.metrics_port)

    commands = {
        "run-seed": run_seed,
        "fork-branch": fork_branch,
        "replay-branch": replay_branch,
        "diff-branches": diff_branches,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
