"""Runs and observes red-team agent sessions for the frontend.

Separate composition root from SimSession: the demo simulation in
`api/sim_session.py` runs entirely in-memory (`LiveChronoDAG`), but the
red-team harness (`agents/redteam/harness.py`) always talks to the real
Postgres/Supabase ChronoDAG via `FINSIM_DB_URL` — see
`sim/main.py::build_simulation_for_branch()`. Unifying those two backends is
a separate piece of work outside this module's scope; a red-team session
here is independent of whatever branch is selected in the main DAG view.

litellm/router calls are blocking, so each session runs in a thread-pool
executor (`run_in_executor`), reporting progress back to the asyncio event
loop via `on_step`/`call_soon_threadsafe` — the same pattern SimSession uses
for its own blocking DAG operations (`asyncio.to_thread`).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_SESSIONS = 50  # bound in-memory history; oldest sessions are evicted


@dataclass
class RedTeamSessionState:
    session_id: str
    status: str  # "running" | "done" | "error"
    from_genesis: bool
    checkpoint_id: str | None
    use_graph: bool
    branch_id: str | None = None
    steps_taken: int = 0
    committed: bool = False
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    step_log: list[dict[str, object]] = field(default_factory=list)


class RedTeamObserver:
    """In-process registry of red-team sessions + pub/sub for live steps —
    mirrors LiveChronoDAG's subscriber pattern (api/live_dag.py) but for
    session step events instead of ChronoDAG events.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, RedTeamSessionState] = {}
        self._order: list[str] = []
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, object]]]] = {}

    def list_sessions(self) -> list[RedTeamSessionState]:
        return [self._sessions[sid] for sid in reversed(self._order)]

    def get_session(self, session_id: str) -> RedTeamSessionState:
        return self._sessions[session_id]

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, object]]:
        q: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue[dict[str, object]]) -> None:
        subs = self._subscribers.get(session_id)
        if subs and q in subs:
            subs.remove(q)

    def _broadcast(self, session_id: str, message: dict[str, object]) -> None:
        for q in list(self._subscribers.get(session_id, [])):
            q.put_nowait(message)

    async def start_session(
        self,
        *,
        from_genesis: bool,
        checkpoint_id: str | None,
        seed: int,
        use_graph: bool,
    ) -> str:
        session_id = uuid.uuid4().hex[:8]
        state = RedTeamSessionState(
            session_id=session_id, status="running",
            from_genesis=from_genesis, checkpoint_id=checkpoint_id, use_graph=use_graph,
        )
        self._sessions[session_id] = state
        self._order.append(session_id)
        if len(self._order) > _MAX_SESSIONS:
            evicted = self._order.pop(0)
            self._sessions.pop(evicted, None)
            self._subscribers.pop(evicted, None)

        loop = asyncio.get_event_loop()

        def on_step(entry: dict[str, object]) -> None:
            step = entry["step"]
            state.steps_taken = step if isinstance(step, int) else int(str(step))
            state.step_log.append(entry)
            message: dict[str, object] = {"type": "step", **entry}
            loop.call_soon_threadsafe(self._broadcast, session_id, message)

        def run() -> None:
            self._run_session_blocking(state, on_step, seed=seed)
            done_message: dict[str, object] = {
                "type": "done", "status": state.status, "committed": state.committed, "error": state.error,
            }
            loop.call_soon_threadsafe(self._broadcast, session_id, done_message)

        loop.run_in_executor(None, run)
        return session_id

    def _run_session_blocking(
        self, state: RedTeamSessionState, on_step: Callable[[dict[str, object]], None], *, seed: int
    ) -> None:
        # Imported here, not at module top-level: pulls in litellm/langgraph
        # (the `redteam` extra), which the base API install doesn't require.
        from agents.redteam.config import RedTeamConfig
        from agents.redteam.harness import run_session, run_session_via_graph
        from sim.config import SimConfig

        try:
            sim_config = SimConfig(seed=seed, num_users=5, num_merchants=1)
            redteam_config = RedTeamConfig(warmup_hours=0.1, session_max_steps=8)
            run_fn = run_session_via_graph if state.use_graph else run_session
            result = run_fn(
                sim_config, redteam_config,
                warmup_checkpoint_id=state.checkpoint_id, from_genesis=state.from_genesis,
                session_id=state.session_id, on_step=on_step,
            )
            state.branch_id = result.branch_id
            state.steps_taken = result.steps_taken
            state.committed = result.committed
            state.status = "done"
        except Exception as exc:  # report to the frontend rather than crash the executor thread
            state.status = "error"
            state.error = str(exc)
