"""Red-team session loop — lockstep (docs/redteam_agent_design.md §1,
locked): checkout/fork -> { observe -> decide (one LLM call) -> act (one
call_tool()) -> observe outcome } -> repeat until commit_strategy or the
per-session step cap.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from agents.redteam.identity import bootstrap_red_agent_context
from agents.redteam.llm_router import build_router, decide_next_action
from agents.redteam.personas import (
    REDTEAM_PERSONA_PROMPT,
    summarize_target_notes,
    summarize_tools,
    summarize_world_view,
)
from sim.chrono.store import PostgresChronoDAG
from sim.main import build_simulation, build_simulation_for_branch
from sim.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from langgraph.graph.state import CompiledStateGraph
    from litellm import Router

    from agents.redteam.config import RedTeamConfig
    from sim.config import SimConfig
    from sim.core.engine import WorldEngineImpl
    from sim.gateway.gateway import ToolGatewayImpl
    from sim.gateway.interfaces import ActorContext

logger = get_logger("finsim.redteam")


@dataclass
class SessionResult:
    branch_id: str
    session_id: str
    steps_taken: int
    committed: bool
    step_log: list[dict[str, object]] = field(default_factory=list)
    # A checkpoint of the branch's state at the moment the session ended
    # (committed or not) — without this there was no checkpoint anywhere
    # on a red-team branch after the initial fork, so "start another
    # session from where this one left off" was impossible: nothing to
    # pass to --checkpoint. Always created, not just on commit, so a
    # session that hit the step cap without committing is still resumable.
    end_checkpoint_id: str | None = None
    # The LLM's own reasoning for its commit_strategy call — written to
    # branch metadata by _record_commit_reasoning() but, before this field
    # existed, never returned to any caller, so nothing (API or UI) could
    # actually display "what did this session find" without a raw SQL
    # query against Postgres.
    commit_reasoning: str | None = None


# How many past steps decide_next_action() gets to see (docs/redteam_agent_design.md
# — bounded so prompt size/token cost doesn't grow unboundedly over a
# session_max_steps=30 run; 12 is enough to remember "I already tried this
# and it failed" without re-explaining the entire session every turn).
_HISTORY_WINDOW = 12


def _history_line(
    step: int, tool_name: str, parameters: dict[str, object],
    success: bool, error_code: str | None, error_message: str | None,
) -> str:
    # Name the account(s) actually involved — a bare "#5 transfer_funds:
    # FAILED" gives the agent no way to tell *which* account it was
    # targeting once that step scrolls out of the (bounded) history
    # window; this is the cheap complement to save_note (below) for
    # remembering what it's already tried against a given target.
    target = ""
    if tool_name in ("transfer_funds", "make_payment"):
        src, dst, amt = (
            parameters.get("source_account_id"), parameters.get("target_account_id"), parameters.get("amount_paise"),
        )
        target = f" [{src} -> {dst}, {amt}p]"
    elif tool_name == "inspect_account":
        target = f" [{parameters.get('account_id')}]"
    return (
        f"#{step} {tool_name}{target}: OK" if success
        else f"#{step} {tool_name}{target}: FAILED ({error_code}) {error_message}"
    )


def _extract_saved_note(data: dict[str, object]) -> str | None:
    """Pull the note text back out of a successful save_note ToolResult.data
    (shaped {"events": [{"note": "..."}]} — see sim/main.py's NoteSaved).
    Used by both run_session() and build_graph()'s act() node to grow the
    session's `notes` list the same way.
    """
    events = data.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        note = events[0].get("note")
        if isinstance(note, str) and note:
            return note
    return None


def _new_session_id() -> str:
    return uuid.uuid4().hex[:8]


def _warmup_checkpoint(sim_config: SimConfig, redteam_config: RedTeamConfig) -> str:
    """Run a short deterministic population warmup on "main" and checkpoint
    it — the mechanism behind the --from-genesis path (docs/redteam_agent_design.md
    §2). Returns the new checkpoint's id.
    """
    from sim.population.agents import PopulationManager
    from sim.population.behaviour import PopulationBehaviourModel
    from sim.population.calibration import calibrate_from_csv
    from sim.population.interfaces import CalibratedParams

    engine, _gateway, chrono = build_simulation(sim_config)

    try:
        params = calibrate_from_csv("data/paysim")
    except FileNotFoundError:
        params = CalibratedParams({}, (), {}, {}, {})

    behaviour_model = PopulationBehaviourModel(params, engine._rng)
    population = PopulationManager(behaviour_model, engine._rng)
    population.create_population(
        num_users=sim_config.num_users, num_merchants=sim_config.num_merchants, engine=engine
    )
    population.start_agent_loops(engine)
    engine._env.run(until=int(redteam_config.warmup_hours * 3600 * 1e9))

    checkpoint = chrono.create_checkpoint(
        branch_id="main",
        event_number=engine._seq_num,
        sim_time_ns=engine.sim_time_ns,
        state_hash=engine.get_state_hash(),
        aggregate_snapshot=engine.get_full_snapshot_bytes(),
        rng_state=engine._rng.get_state(),
    )
    logger.info(
        "Warmup checkpoint created", checkpoint_id=checkpoint.checkpoint_id,
        sim_time_ns=checkpoint.sim_time_ns, event_number=checkpoint.event_number,
    )
    return str(checkpoint.checkpoint_id)


def _record_commit_reasoning(chrono: PostgresChronoDAG, branch_id: str, reasoning: str) -> None:
    """Write the LLM's own reasoning for its commit_strategy call onto the
    branch's metadata — commit_strategy_handler (sim/main.py) only ever set
    origin="committed", the actual summary text (why the agent believes
    this demonstrates something) was visible live in the UI step feed but
    never written anywhere durable. Separate from commit_strategy_handler
    itself because the handler only sees `parameters`, not the `reasoning`
    field NextAction carries — the harness/graph node already has it.
    """
    branch = chrono.checkout(branch_id).branch
    chrono.update_branch_metadata(branch_id, {**branch.metadata, "commit_reasoning": reasoning})


def _checkpoint_branch_end(
    chrono: PostgresChronoDAG, engine: WorldEngineImpl, branch_id: str, *, session_id: str, committed: bool
) -> str:
    """Checkpoint the branch's state at session end (committed or not) so a
    later session can fork from exactly where this one left off — mirrors
    _warmup_checkpoint()'s shape but for the red-team branch itself, not
    "main". Before this, nothing ever checkpointed a red-team branch after
    its initial fork, so "continue from this session" had no checkpoint_id
    to actually use.
    """
    checkpoint = chrono.create_checkpoint(
        branch_id=branch_id,
        event_number=engine._seq_num,
        sim_time_ns=engine.sim_time_ns,
        state_hash=engine.get_state_hash(),
        aggregate_snapshot=engine.get_full_snapshot_bytes(),
        rng_state=engine._rng.get_state(),
        metadata={"origin": "session_end", "session_id": session_id, "committed": committed},
    )
    logger.info(
        "Session-end checkpoint created", checkpoint_id=checkpoint.checkpoint_id,
        branch_id=branch_id, session_id=session_id, committed=committed,
    )
    return str(checkpoint.checkpoint_id)


def _pool_notes_from_branches(chrono: PostgresChronoDAG, branch_ids: list[str]) -> list[str]:
    """Pool `target_notes` (from save_note) and `commit_reasoning` (from
    commit_strategy, see _record_commit_reasoning) off any number of other
    red-team branches — not just the one this session forked from.

    Forking from a checkpoint restores engine STATE (account balances
    exactly as they were) but not KNOWLEDGE — without this, a "continued"
    session would have no idea what a prior pass already flagged and would
    rediscover it turn by turn. This generalizes that beyond direct
    lineage: several independent sessions run off the *same* warmup
    checkpoint each find something different and commit separately —
    there was previously no way to start a new session that combines what
    all of them found, only whichever single branch you happened to fork
    from. Non-red-team branch_ids (e.g. "main") and unknown/bad ids are
    silently skipped rather than failing the whole session start — this is
    best-effort context, not a hard dependency.
    """
    notes: list[str] = []
    for branch_id in branch_ids:
        if not branch_id or not branch_id.startswith("red-team/"):
            continue
        try:
            metadata = chrono.checkout(branch_id).branch.metadata
        except Exception:
            logger.warning("Could not pool notes from branch (skipping)", branch_id=branch_id)
            continue
        parent_notes = metadata.get("target_notes", [])
        if isinstance(parent_notes, list):
            notes.extend(f"[from {branch_id}] {n}" for n in parent_notes)
        commit_reasoning = metadata.get("commit_reasoning")
        if isinstance(commit_reasoning, str) and commit_reasoning:
            notes.append(f"[from {branch_id}, prior session's commit_strategy] {commit_reasoning}")
    return notes


def _fork_session_branch(sim_config: SimConfig, checkpoint_id: str, session_id: str) -> tuple[str, str | None]:
    if not sim_config.db_url:
        raise SystemExit("No database URL: set FINSIM_DB_URL in the environment/.env")
    chrono = PostgresChronoDAG(sim_config.db_url)
    branch = chrono.fork(
        checkpoint_id=checkpoint_id,
        branch_id=f"red-team/{session_id}",
        metadata={"origin": "agent_experiment"},
    )
    return str(branch.branch_id), branch.parent_branch_id


def _prepare_session(
    sim_config: SimConfig,
    redteam_config: RedTeamConfig,
    warmup_checkpoint_id: str | None,
    from_genesis: bool,
    session_id: str,
    router: Router | None,
    identity_file: Path | None,
    pool_from_branch_ids: list[str] | None = None,
) -> tuple[WorldEngineImpl, ToolGatewayImpl, ActorContext, Router, str, str, PostgresChronoDAG, list[str]]:
    """Shared setup for both run_session() and run_session_via_graph():
    resolve/create the warmup checkpoint, fork the session branch, rebuild
    the engine on it, and bootstrap the agent's identity + LLM router.

    Returns `chrono` too (previously discarded as `_chrono`) — both
    callers need it at session end to checkpoint the branch and record
    commit_strategy's reasoning (see _checkpoint_branch_end/
    _record_commit_reasoning), not just at setup time. Also returns
    `seed_notes` — forking from a checkpoint restores engine STATE, not
    the prior session's KNOWLEDGE of what it already found. The branch
    actually forked from is always pooled automatically; `pool_from_branch_ids`
    lets the caller pull in *additional* red-team branches' findings too
    (e.g. several independent sessions run off the same warmup checkpoint,
    each finding something different — see _pool_notes_from_branches).
    """
    if from_genesis:
        warmup_checkpoint_id = _warmup_checkpoint(sim_config, redteam_config)
    elif warmup_checkpoint_id is None:
        raise ValueError("warmup_checkpoint_id is required unless from_genesis=True")

    branch_id, parent_branch_id = _fork_session_branch(sim_config, warmup_checkpoint_id, session_id)
    engine, gateway, chrono = build_simulation_for_branch(sim_config, branch_id)
    pool_ids = [*([parent_branch_id] if parent_branch_id else []), *(pool_from_branch_ids or [])]
    seed_notes = _pool_notes_from_branches(chrono, list(dict.fromkeys(pool_ids)))  # de-dupe, keep order

    if identity_file is not None:
        ctx = bootstrap_red_agent_context(
            branch_id=branch_id, session_id=session_id, identity_file=identity_file
        )
    else:
        ctx = bootstrap_red_agent_context(branch_id=branch_id, session_id=session_id)
    router = router or build_router(redteam_config)
    tools_summary = summarize_tools(gateway.list_tools(ctx))
    return engine, gateway, ctx, router, tools_summary, branch_id, chrono, seed_notes


def run_session(
    sim_config: SimConfig,
    redteam_config: RedTeamConfig,
    warmup_checkpoint_id: str | None = None,
    from_genesis: bool = False,
    session_id: str | None = None,
    router: Router | None = None,
    identity_file: Path | None = None,
    on_step: Callable[[dict[str, object]], None] | None = None,
    pool_from_branch_ids: list[str] | None = None,
) -> SessionResult:
    """Run one lockstep red-team session and return its outcome.

    `router` and `identity_file` are injectable for tests (see
    agents/redteam/tests/test_harness.py) — `router` defaults to a real
    litellm.Router built from providers.yaml, `identity_file` defaults to
    bootstrap_red_agent_context()'s own default (the real, gitignored
    agents/redteam/.persona_identity.json).

    `on_step`, if given, is called synchronously after each step with the
    same dict appended to result.step_log — the hook api/redteam_session.py
    uses to broadcast live steps to the frontend (docs/redteam_agent_design.md
    §8). Optional so run_session() has no observability dependency by default.

    `pool_from_branch_ids`, if given, pulls target_notes/commit_reasoning
    from any number of other red-team branches into this session's initial
    notes — not just the one branch it forks from (see
    _pool_notes_from_branches). Lets several independent sessions' findings
    get combined into one continuing session.

    See also run_session_via_graph() — the LangGraph-orchestrated equivalent
    of this same loop (docs/redteam_agent_design.md §6 Phase 6).
    """
    session_id = session_id or _new_session_id()
    engine, gateway, ctx, router, tools_summary, branch_id, chrono, seed_notes = _prepare_session(
        sim_config, redteam_config, warmup_checkpoint_id, from_genesis, session_id, router, identity_file,
        pool_from_branch_ids,
    )

    result = SessionResult(branch_id=branch_id, session_id=session_id, steps_taken=0, committed=False)
    last_outcome: str | None = None
    history: list[str] = []
    notes: list[str] = list(seed_notes)  # own save_note calls + anything pooled from a parent session

    for _step in range(redteam_config.session_max_steps):
        view = engine.get_world_view(ctx.actor_id, ctx.actor_role)
        world_summary = f"{tools_summary}\n\n{summarize_world_view(view)}\n\n{summarize_target_notes(notes)}"

        action = decide_next_action(
            router, redteam_config, world_summary, REDTEAM_PERSONA_PROMPT, last_outcome,
            history=tuple(history[-_HISTORY_WINDOW:]),
        )
        tool_result = gateway.call_tool(action.tool_name, action.parameters, ctx)
        result.steps_taken += 1
        if action.tool_name == "save_note" and tool_result.success:
            note = _extract_saved_note(tool_result.data)
            if note:
                notes.append(note)
        step_entry: dict[str, object] = {
            "step": result.steps_taken,
            "tool_name": action.tool_name,
            "parameters": action.parameters,
            "reasoning": action.reasoning,
            "success": tool_result.success,
            "error_code": tool_result.error_code,
            # Was missing entirely — the UI had error_code ("INTERNAL_ERROR",
            # "LIMIT_EXCEEDED", ...) but never the actual message, so there
            # was no way to tell from the step feed whether a failure was an
            # expected business rejection or a real bug. See
            # sim/gateway/errors.py for how INTERNAL_ERROR's message is
            # written to make that distinction obvious on its own.
            "error_message": tool_result.error_message,
            "provider_model": action.provider_model,
            "latency_ms": action.latency_ms,
        }
        result.step_log.append(step_entry)
        if on_step is not None:
            on_step(step_entry)
        last_outcome = (
            f"{action.tool_name} succeeded: {tool_result.data}" if tool_result.success
            else f"{action.tool_name} FAILED ({tool_result.error_code}): {tool_result.error_message}"
        )
        history.append(_history_line(
            result.steps_taken, action.tool_name, action.parameters, tool_result.success,
            tool_result.error_code, tool_result.error_message,
        ))
        logger.info(
            "Session step", session_id=session_id, step=result.steps_taken,
            tool_name=action.tool_name, success=tool_result.success,
        )

        if action.tool_name == "commit_strategy" and tool_result.success:
            result.committed = True
            result.commit_reasoning = action.reasoning
            _record_commit_reasoning(chrono, branch_id, action.reasoning)
            break

    result.end_checkpoint_id = _checkpoint_branch_end(
        chrono, engine, branch_id, session_id=session_id, committed=result.committed
    )
    logger.info(
        "Session finished", session_id=session_id, branch_id=branch_id,
        steps_taken=result.steps_taken, committed=result.committed,
        end_checkpoint_id=result.end_checkpoint_id,
    )
    return result


class RedTeamGraphState(TypedDict):
    """LangGraph state for build_graph(). `action`/`tool_result` use plain
    dicts (not the agents.redteam.llm_router.NextAction dataclass) so that
    sim/gateway/adapters.py's LangGraphAdapter — which must not import
    anything from agents/, see that module's docstring — can consume
    `state["action"]` without a cross-boundary import.
    """

    world_summary: str
    last_outcome: str | None
    history: list[str]
    notes: list[str]  # from save_note — see personas.summarize_target_notes()
    action: dict[str, object] | None
    tool_result: object | None  # sim.gateway.interfaces.ToolResult at runtime
    steps_taken: int
    committed: bool
    commit_reasoning: str | None


def build_graph(
    engine: WorldEngineImpl,
    gateway: ToolGatewayImpl,
    ctx: ActorContext,
    router: Router,
    redteam_config: RedTeamConfig,
    tools_summary: str,
    chrono: PostgresChronoDAG,
    on_step: Callable[[dict[str, object]], None] | None = None,
) -> CompiledStateGraph:
    """LangGraph-orchestrated version of run_session()'s loop: observe ->
    decide -> act, looping back to observe until commit_strategy succeeds or
    session_max_steps is reached. Thin wrapper over the same primitives
    run_session() uses (decide_next_action, LangGraphAdapter.as_tool_node) —
    built after that bare loop was proven working (docs/redteam_agent_design.md
    §6 Phase 6), not before.

    `on_step`, if given, mirrors run_session()'s hook — called after each
    "act" node with the same step-entry shape.
    """
    from langgraph.graph import END, START, StateGraph

    from sim.gateway.adapters import LangGraphAdapter

    act_node = LangGraphAdapter(gateway).as_tool_node(ctx)

    def observe(state: RedTeamGraphState) -> dict[str, object]:
        view = engine.get_world_view(ctx.actor_id, ctx.actor_role)
        notes_block = summarize_target_notes(state["notes"])
        return {"world_summary": f"{tools_summary}\n\n{summarize_world_view(view)}\n\n{notes_block}"}

    def decide(state: RedTeamGraphState) -> dict[str, object]:
        next_action = decide_next_action(
            router, redteam_config, state["world_summary"], REDTEAM_PERSONA_PROMPT, state["last_outcome"],
            history=tuple(state["history"][-_HISTORY_WINDOW:]),
        )
        return {
            "action": {
                "tool_name": next_action.tool_name,
                "parameters": next_action.parameters,
                "reasoning": next_action.reasoning,
                "provider_model": next_action.provider_model,
                "latency_ms": next_action.latency_ms,
            }
        }

    def act(state: RedTeamGraphState) -> dict[str, object]:
        act_result = act_node(state)
        tool_result = act_result["tool_result"]
        action = state["action"]
        assert action is not None
        outcome = (
            f"{action['tool_name']} succeeded: {tool_result.data}" if tool_result.success
            else f"{action['tool_name']} FAILED ({tool_result.error_code}): {tool_result.error_message}"
        )
        committed = action["tool_name"] == "commit_strategy" and tool_result.success
        commit_reasoning = state.get("commit_reasoning")
        if committed:
            commit_reasoning = str(action.get("reasoning", ""))
            _record_commit_reasoning(chrono, ctx.branch_id, commit_reasoning)
        steps_taken = state["steps_taken"] + 1
        if on_step is not None:
            on_step({
                "step": steps_taken,
                "tool_name": action["tool_name"],
                "parameters": action["parameters"],
                "reasoning": action.get("reasoning", ""),
                "success": tool_result.success,
                "error_code": tool_result.error_code,
                "error_message": tool_result.error_message,  # was missing — see run_session()'s step_entry
                "provider_model": action.get("provider_model"),
                "latency_ms": action.get("latency_ms"),
            })
        notes = state["notes"]
        if action["tool_name"] == "save_note" and tool_result.success:
            note = _extract_saved_note(tool_result.data)
            if note:
                notes = [*notes, note]

        action_params = action["parameters"]
        history_params = action_params if isinstance(action_params, dict) else {}
        return {
            "tool_result": tool_result,
            "last_outcome": outcome,
            "history": [*state["history"], _history_line(
                steps_taken, str(action["tool_name"]), history_params, tool_result.success,
                tool_result.error_code, tool_result.error_message,
            )],
            "notes": notes,
            "steps_taken": steps_taken,
            "committed": committed,
            "commit_reasoning": commit_reasoning,
        }

    def route_after_act(state: RedTeamGraphState) -> str:
        if state["committed"] or state["steps_taken"] >= redteam_config.session_max_steps:
            return str(END)
        return "observe"

    graph = StateGraph(RedTeamGraphState)
    graph.add_node("observe", observe)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_edge(START, "observe")
    graph.add_edge("observe", "decide")
    graph.add_edge("decide", "act")
    graph.add_conditional_edges("act", route_after_act, {END: END, "observe": "observe"})
    return graph.compile()


def run_session_via_graph(
    sim_config: SimConfig,
    redteam_config: RedTeamConfig,
    warmup_checkpoint_id: str | None = None,
    from_genesis: bool = False,
    session_id: str | None = None,
    router: Router | None = None,
    identity_file: Path | None = None,
    on_step: Callable[[dict[str, object]], None] | None = None,
    pool_from_branch_ids: list[str] | None = None,
) -> SessionResult:
    """LangGraph-orchestrated equivalent of run_session() — same setup, same
    step semantics, orchestrated as a StateGraph instead of a Python loop.
    See run_session()'s docstring for `pool_from_branch_ids`.
    """
    session_id = session_id or _new_session_id()
    engine, gateway, ctx, router, tools_summary, branch_id, chrono, seed_notes = _prepare_session(
        sim_config, redteam_config, warmup_checkpoint_id, from_genesis, session_id, router, identity_file,
        pool_from_branch_ids,
    )
    graph = build_graph(engine, gateway, ctx, router, redteam_config, tools_summary, chrono, on_step=on_step)

    initial_state: RedTeamGraphState = {
        "world_summary": "", "last_outcome": None, "history": [], "notes": list(seed_notes), "action": None,
        "tool_result": None, "steps_taken": 0, "committed": False, "commit_reasoning": None,
    }
    final_state = graph.invoke(initial_state)

    result = SessionResult(
        branch_id=branch_id, session_id=session_id,
        steps_taken=final_state["steps_taken"], committed=final_state["committed"],
        commit_reasoning=final_state.get("commit_reasoning"),
    )
    result.end_checkpoint_id = _checkpoint_branch_end(
        chrono, engine, branch_id, session_id=session_id, committed=result.committed
    )
    logger.info(
        "Session finished (graph)", session_id=session_id, branch_id=branch_id,
        steps_taken=result.steps_taken, committed=result.committed,
        end_checkpoint_id=result.end_checkpoint_id,
    )
    return result
