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

    from sim.core.interfaces import AccountType, Command, CommandResult, TransactionType
    from sim.gateway.interfaces import Capability, ToolRejection, ToolSpec

    def _describe_rejection(command_desc: str, result: CommandResult) -> ToolRejection:
        """Turn a failed CommandResult into a ToolRejection carrying the
        domain's own reason_code/decline_code — not just a generic
        INTERNAL_ERROR — so the agent learns exactly which control it
        tripped (LIMIT_EXCEEDED, INSUFFICIENT_FUNDS, ...) instead of "an
        error happened, guess why."
        """
        if not result.events:
            # _execute_transfer/_execute_payment return an empty events
            # list (not a rejection event) when source/target account_id
            # doesn't resolve to a real account at all — a different
            # failure mode than a validated-but-declined command.
            return ToolRejection(
                "ACCOUNT_NOT_FOUND",
                f"{command_desc} rejected: source_account_id or target_account_id does not "
                "resolve to any account on this branch. Double-check the ids from your world view."
            )
        codes = []
        for e in result.events:
            code = getattr(e, "reason_code", None) or getattr(e, "decline_code", None) or type(e).__name__
            detail = getattr(e, "detail", None) or getattr(e, "reason", None) or ""
            codes.append(f"{code}" + (f": {detail}" if detail else ""))
        return ToolRejection(codes[0].split(":")[0], f"{command_desc} rejected — {'; '.join(codes)}")

    def _optional_int(params: dict[str, object], key: str, default: int) -> int:
        val = params.get(key, default)
        try:
            return int(str(val))
        except (TypeError, ValueError):
            raise ToolRejection("INVALID_PARAMETER", f"'{key}' must be an integer, got {val!r}") from None

    def _require_account_id(params: dict[str, object], key: str) -> str:
        val = params.get(key)
        if not val:
            raise ToolRejection("MISSING_PARAMETER", f"'{key}' is required and was not provided")
        return str(val)

    def _require_amount_paise(params: dict[str, object]) -> int:
        # This used to be `int(str(params.get("amount_paise")))` — a missing
        # amount_paise silently became int("None"), a raw ValueError the
        # gateway's generic exception handler collapses into an
        # uninformative INTERNAL_ERROR (that's what a red-team agent was
        # actually hitting: not a rejected transfer, a malformed tool call
        # it got no real signal about). Validate explicitly instead.
        val = params.get("amount_paise")
        if val is None:
            raise ToolRejection("MISSING_PARAMETER", "'amount_paise' is required and was not provided")
        try:
            return int(str(val))
        except (TypeError, ValueError):
            raise ToolRejection(
                "INVALID_PARAMETER", f"'amount_paise' must be an integer (paise), got {val!r}"
            ) from None

    # 1. create_account
    #
    # Was a pure no-op stub (`return []`) that never called
    # engine.create_account() — every call silently did nothing while
    # reporting success, so a red-team agent would never accumulate any
    # accounts of its own (get_world_view() filters to owner_id==actor_id,
    # see sim/core/engine.py::get_world_view) and would just loop
    # create_account -> inspect_account forever, never reaching
    # transfer_funds/make_payment. Fixed to genuinely create an account
    # owned by the calling actor.
    #
    # kyc_level=0 (the default here) is deliberately the *lowest* tier —
    # KYC_DAILY_LIMITS[0] is the tightest daily cap (sim/core/account.py),
    # i.e. exactly the "exploit KYC/daily-limit edges" surface
    # REDTEAM_PERSONA_PROMPT asks the agent to probe. initial_balance_paise
    # defaults to comfortably more than that daily limit so there's
    # actually room to test structuring across multiple transfers/days.
    def create_account_handler(context, params, engine):
        account_id = str(uuid.uuid4())
        account_type_str = params.get("account_type", AccountType.PERSONAL.value)
        try:
            account_type = AccountType(account_type_str)
        except ValueError:
            valid = ", ".join(t.value for t in AccountType)
            raise ToolRejection(
                "INVALID_PARAMETER", f"'account_type' {account_type_str!r} is not valid — choose one of: {valid}"
            ) from None
        engine.create_account(
            account_id=account_id,
            owner_id=context.actor_id,
            account_type=account_type,
            initial_balance_paise=_optional_int(params, "initial_balance_paise", 20_00_000),
            kyc_level=_optional_int(params, "kyc_level", 0),
        )
        view = engine.get_world_view(context.actor_id, context.actor_role)
        return [acc for acc in view.accounts if acc.account_id == account_id]

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
            source_account_id=_require_account_id(params, "source_account_id"),
            target_account_id=_require_account_id(params, "target_account_id"),
            amount_paise=_require_amount_paise(params),
            idempotency_key=str(params.get("idempotency_key", uuid.uuid4()))
        )
        result = engine.execute_command(cmd)
        if not result.success:
            # execute_command() never raises on a *business* rejection
            # (insufficient funds, frozen account, over daily limit, ...) —
            # it returns CommandResult(success=False, events=(RejectionEvent,)).
            # The old handler returned `.events` unconditionally, so the
            # gateway reported EVERY rejected transfer as a success. That's
            # the one signal a red-team agent most needs to read correctly,
            # so raise ToolRejection with the domain's own reason_code
            # instead (see _describe_rejection) — not just "it failed."
            raise _describe_rejection("Transfer", result)
        return result.events

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
            source_account_id=_require_account_id(params, "source_account_id"),
            target_account_id=_require_account_id(params, "target_account_id"),
            amount_paise=_require_amount_paise(params),
            idempotency_key=str(params.get("idempotency_key", uuid.uuid4())),
            gateway_hint=str(params.get("gateway_id", ""))
        )
        result = engine.execute_command(cmd)
        if not result.success:  # same rejection-vs-exception gap as transfer_funds above
            raise _describe_rejection("Payment", result)
        return result.events

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
        # Was falling through to `return []` — an empty-but-successful
        # ToolResult, indistinguishable from "this account has no
        # displayable fields" rather than "you don't own an account with
        # that id". Fail loudly instead so the agent's next decision is
        # informed by an honest error, not a silent no-op.
        raise ToolRejection("ACCOUNT_NOT_FOUND", f"No visible account with id {acc_id!r} on this branch")

    registry.register_tool(
        ToolSpec("inspect_account", "Inspect account details", frozenset({Capability.VIEW_OWN_ACCOUNT}), {}),
        inspect_account_handler
    )


def _register_redteam_tools(registry: ToolRegistry, engine: WorldEngineImpl, chrono: PostgresChronoDAG) -> None:
    """Tools only meaningful on a red-team session's branch — not registered
    by build_simulation() (main), only by build_simulation_for_branch().
    See docs/redteam_agent_design.md §3.
    """
    from dataclasses import dataclass

    from sim.gateway.interfaces import Capability, ToolRejection, ToolSpec

    # How much the agent must actually have DONE before commit_strategy is
    # allowed to end the session. Real sessions were committing at step
    # 3-6 of 30 off a single successful transfer, describing it as
    # "demonstrated laundering ... evaded controls" — the prose bar in the
    # persona prompt ("commit only once you've actually tried something")
    # is trivially satisfied by any one success, so it never bound. A
    # fraud pattern worth committing is multi-step by definition
    # (structuring, layering, mule routing all mean N movements that
    # individually look fine), so the floor is enforced here where it
    # can't be talked around, not just asked for in the prompt.
    _MIN_VALUE_MOVEMENTS_TO_COMMIT = 3

    def _count_value_movements(context) -> int:
        """How many times this actor has actually moved value on this branch.

        Counted from the ChronoDAG's own event log, not from anything the
        agent reports about itself: chrono.checkout() returns every event
        committed after the branch's latest checkpoint, which for a
        red-team branch (forked from a checkpoint, not re-checkpointed
        until session end) is exactly this session's own history.
        AccountDebited is the signal — both _execute_transfer and
        _execute_payment emit exactly one per successful movement, and a
        rejected transfer/payment emits none, so this counts real
        movements and can't be inflated by failed attempts.
        """
        try:
            pending = chrono.checkout(context.branch_id).pending_events
        except Exception:
            return 0
        return sum(
            1 for e in pending
            if e.event_type == "AccountDebited" and e.actor_id == context.actor_id
        )

    def commit_strategy_handler(context, params, engine):
        pattern = str(params.get("pattern", "")).strip()
        impact = str(params.get("impact", "")).strip()
        if not pattern:
            raise ToolRejection(
                "MISSING_PARAMETER",
                "'pattern' is required: name the fraud pattern class you are committing "
                "(e.g. 'structuring', 'layering through intermediary', 'mule cash-out') "
                "and the concrete route, not a restatement that a transfer succeeded.",
            )
        if not impact:
            raise ToolRejection(
                "MISSING_PARAMETER",
                "'impact' is required: state what a defender should be alarmed by — "
                "how much value moved, through how many hops/accounts, and which control "
                "it went around.",
            )
        movements = _count_value_movements(context)
        if movements < _MIN_VALUE_MOVEMENTS_TO_COMMIT:
            raise ToolRejection(
                "INSUFFICIENT_EVIDENCE",
                f"You have only {movements} successful value movement(s) on this branch; "
                f"{_MIN_VALUE_MOVEMENTS_TO_COMMIT} is the minimum to commit a pattern. A single "
                "transfer that succeeded is not a fraud pattern — it is one transaction. Build "
                "the multi-step pattern (more hops, more accounts, or repeated movement under a "
                "threshold) and commit once you can point at the sequence.",
            )
        branch = chrono.checkout(context.branch_id).branch
        chrono.update_branch_metadata(context.branch_id, {
            **branch.metadata,
            "origin": "committed",
            "committed_pattern": pattern,
            "committed_impact": impact,
            "committed_value_movements": movements,
        })
        return []

    registry.register_tool(
        ToolSpec(
            "commit_strategy",
            "End the session and tag this branch as a committed red-team finding. Requires "
            "'pattern' (the fraud pattern class + concrete route you demonstrated) and 'impact' "
            "(value moved, hops, which control it bypassed). Rejected with INSUFFICIENT_EVIDENCE "
            f"until you have made at least {_MIN_VALUE_MOVEMENTS_TO_COMMIT} successful "
            "transfers/payments — one successful transaction is not a pattern.",
            frozenset({Capability.FORK_BRANCH}),
            {},
            rate_limit_tier="branch_op",
        ),
        commit_strategy_handler,
    )

    @dataclass
    class TimeAdvanced:
        new_sim_time_ns: int
        sim_day: int

    def advance_time_handler(context, params, engine):
        # A forked red-team branch is reconstructed with an EMPTY scheduler
        # queue (build_simulation_for_branch above) and execute_command()
        # never touches the clock — so without this tool sim_time_ns is
        # frozen for the entire session. That silently made a whole class
        # of the patterns the persona prompt asks for impossible: daily
        # counters reset lazily on a day boundary (Account.check_daily_limit),
        # so with a frozen clock every account's daily limit is a one-shot
        # budget for the session and "structuring across days" / "cash-out
        # velocity" / any time-dependent pattern cannot be expressed at all.
        # Safe to expose: env.run(until=...) on an empty queue processes
        # zero events and just moves _now forward (sim/scheduler/env.py),
        # so this can't trip the superlinear event-growth issue documented
        # in CLAUDE.md — that's about scheduling population events, and a
        # red-team branch has none in flight.
        hours = params.get("hours")
        if hours is None:
            raise ToolRejection("MISSING_PARAMETER", "'hours' is required (number of sim hours to advance)")
        try:
            hours_f = float(str(hours))
        except (TypeError, ValueError):
            raise ToolRejection("INVALID_PARAMETER", f"'hours' must be a number, got {hours!r}") from None
        if not 0 < hours_f <= 720:
            raise ToolRejection(
                "INVALID_PARAMETER", f"'hours' must be > 0 and <= 720 (30 days), got {hours_f}"
            )
        target_ns = int(engine._env.now + hours_f * 3600 * 1e9)
        engine._env.run(until=target_ns)
        return [TimeAdvanced(new_sim_time_ns=engine._env.now, sim_day=engine._env.now // 86_400_000_000_000)]

    registry.register_tool(
        ToolSpec(
            "advance_time",
            "Advance the simulation clock by 'hours'. Nothing else moves the clock on this "
            "branch — it is frozen unless you call this. Crossing a sim-day boundary resets "
            "every account's daily transaction counters, which is the only way to get a fresh "
            "daily limit or to express any pattern that depends on timing/velocity.",
            frozenset(),
            {},
        ),
        advance_time_handler,
    )

    @dataclass
    class NoteSaved:
        note: str

    def save_note_handler(context, params, engine):
        # Now that RED_AGENT can see (and target) every account on the
        # branch, not just its own, it needs somewhere to write down which
        # ones it's actually building a strategy around — the rolling
        # step history (agents/redteam/harness.py::_HISTORY_WINDOW) is
        # short and doesn't name the account involved, so anything beyond
        # ~12 steps ago is gone unless it's explicitly saved here. Notes
        # are durably written to the branch's own ChronoDAG metadata (same
        # mechanism commit_strategy uses) — inspectable after the session
        # ends, not just held in the LLM's own context — and the harness
        # also echoes them into every subsequent turn's prompt.
        note = str(params.get("note", "")).strip()
        if not note:
            raise ToolRejection("MISSING_PARAMETER", "'note' is required and must be a non-empty string")
        branch = chrono.checkout(context.branch_id).branch
        existing = branch.metadata.get("target_notes", [])
        notes = [*existing, note] if isinstance(existing, list) else [note]
        chrono.update_branch_metadata(context.branch_id, {**branch.metadata, "target_notes": notes})
        return [NoteSaved(note=note)]

    registry.register_tool(
        ToolSpec(
            "save_note",
            "Save a persistent note about a specific account/person you're building a fraud "
            "strategy around — name the account_id, what you've observed about it, and your "
            "plan. Notes persist for the rest of this session and are shown to you every turn, "
            "so use this instead of relying on your own memory across steps.",
            frozenset(),
            {},
        ),
        save_note_handler,
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
