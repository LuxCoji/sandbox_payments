"""FastAPI layer for the FinSim frontend demo.

Sits behind nothing but the `sim.*` public surfaces (engine, population,
scheduler) plus the local `api.live_dag` / `api.sim_session` helpers — see
those modules' docstrings for why this exists outside `sim/` rather than as
a sixth subsystem.

Run with:  uv run uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import uuid
SERVER_RUN_ID = uuid.uuid4().hex

import dataclasses
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.redteam_session import RedTeamObserver
from api.sim_session import SimSession

session: SimSession | None = None
redteam_observer = RedTeamObserver()
_redteam_chrono_conn = None  # PostgresChronoDAG | None — lazily opened, reused across polls


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session
    session = SimSession(seed=42, num_users=60, num_merchants=8)
    session.start()
    yield
    if session:
        await session.stop()


app = FastAPI(title="FinSim API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _session() -> SimSession:
    if session is None:
        raise HTTPException(503, "Simulation not started")
    return session


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/branches")
async def list_branches():
    # The demo simulation and red-team sessions are two separate ChronoDAG
    # backends (in-memory LiveChronoDAG vs. real Postgres) — merge red-team
    # branches in here too so a forked red-team session actually shows up
    # in the graph instead of only existing in the DB. They render as
    # independent (parentless) lanes: their real Postgres parent is a
    # synthetic "demo-export/*" bridge branch that has no meaning in this
    # graph, so surfacing it would just be a dangling connector.
    branches = _session().list_branch_graph()
    branches.extend(await asyncio.to_thread(_list_redteam_branches))
    return branches


def _get_redteam_chrono():
    # Polled every few seconds by the frontend's branch-list refresh —
    # opening a fresh psycopg connection (plus PostgresChronoDAG's
    # CREATE-TABLE-IF-NOT-EXISTS setup) on every poll would be wasteful, so
    # this is cached at module scope and reused, mirroring the `_session()`
    # global-singleton pattern above.
    global _redteam_chrono_conn
    if _redteam_chrono_conn is None:
        from sim.chrono.store import PostgresChronoDAG
        from sim.config import SimConfig

        db_url = SimConfig().db_url
        if not db_url:
            return None
        _redteam_chrono_conn = PostgresChronoDAG(db_url)
    return _redteam_chrono_conn


def _list_redteam_branches() -> list[dict]:
    chrono = _get_redteam_chrono()
    if chrono is None:
        return []

    with chrono.conn.cursor() as cur:
        # Join with checkpoints to find the exact event number this branch was forked from,
        # and the metadata to see which demo checkpoint it originated from.
        cur.execute(
            '''SELECT b.branch_id, b.head_seq_num, b.created_at_ns, c.event_number, c.metadata
               FROM branches b
               LEFT JOIN checkpoints c ON b.parent_checkpoint_id = c.checkpoint_id
               WHERE b.branch_id LIKE 'red-team/%' ORDER BY b.created_at_ns'''
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT branch_id, event_number FROM checkpoints WHERE branch_id LIKE 'red-team/%'"
        )
        checkpoints_by_branch: dict[str, list[int]] = {}
        for branch_id, event_number in cur.fetchall():
            checkpoints_by_branch.setdefault(branch_id, []).append(event_number)

    demo_checkpoints = set(_session().dag._checkpoints_by_id.keys())

    return [
        {
            "branch_id": branch_id,
            "name": f"🔴 {branch_id.removeprefix('red-team/')}",
            # Only attach to 'main' if the origin checkpoint actually exists in THIS session's memory
            "parent_branch_id": "main" if (c_meta and c_meta.get("server_run_id") == SERVER_RUN_ID) else None,
            "parent_checkpoint_id": None,
            "fork_seq_num": fork_seq_num or 0,
            "head_seq_num": head_seq_num,
            "live": False,  # forked branches never auto-run — static except for agent actions
            "seed_offset": 0,
            "created_at_ns": created_at_ns,
            "checkpoint_seq_nums": sorted(checkpoints_by_branch.get(branch_id, [])),
            "commit_reasoning": c_meta.get("commit_reasoning", "No reasoning recorded") if c_meta else "No reasoning recorded"
        }
        for branch_id, head_seq_num, created_at_ns, fork_seq_num, c_meta in rows
    ]


@app.get("/api/branches/{branch_id}/state")
async def branch_state(branch_id: str):
    try:
        return _session().branch_summary(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}") from None


@app.get("/api/branches/{branch_id}/accounts")
async def branch_accounts(branch_id: str):
    try:
        return _session().accounts(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}") from None


@app.get("/api/branches/{branch_id}/accounts/{account_id}/events")
async def account_events(branch_id: str, account_id: str):
    try:
        return _session().account_events(branch_id, account_id)
    except KeyError:
        raise HTTPException(404, "Unknown branch or account") from None


@app.get("/api/branches/{branch_id}/events")
async def branch_events(branch_id: str, since_seq: int = 0, limit: int = 200):
    try:
        return _session().recent_events(branch_id, since_seq, limit)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}") from None


@app.get("/api/branches/{branch_id}/breakdown")
async def branch_breakdown(branch_id: str):
    try:
        return _session().event_breakdown(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}") from None


@app.get("/api/branches/{branch_id}/checkpoints")
async def branch_checkpoints(branch_id: str):
    try:
        return _session().checkpoints(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}") from None


@app.post("/api/branches/{branch_id}/checkpoint")
async def make_checkpoint(branch_id: str):
    try:
        return _session().create_checkpoint(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}") from None


class ForkRequest(BaseModel):
    parent_branch_id: str = "main"
    name: str
    checkpoint_id: str | None = None  # omit to fork from the current live head


@app.post("/api/branches/fork")
async def fork_branch(req: ForkRequest):
    try:
        return _session().fork(req.parent_branch_id, req.name, req.checkpoint_id)
    except KeyError:
        raise HTTPException(404, "Unknown branch or checkpoint") from None


class ChaosRequest(BaseModel):
    action: str
    params: dict = {}


@app.post("/api/branches/{branch_id}/chaos")
async def chaos(branch_id: str, req: ChaosRequest):
    try:
        return _session().apply_chaos(branch_id, req.action, req.params)
    except KeyError as e:
        raise HTTPException(404, f"Unknown branch/account: {e}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/diff")
async def diff(branch_a: str, branch_b: str):
    try:
        return _session().diff(branch_a, branch_b)
    except KeyError:
        raise HTTPException(404, "Unknown branch") from None


class PauseRequest(BaseModel):
    paused: bool


@app.post("/api/pause")
async def pause(req: PauseRequest):
    _session().set_paused(req.paused)
    return {"paused": req.paused}


class ResetRequest(BaseModel):
    seed: int = 42
    num_users: int = 60
    num_merchants: int = 8


@app.post("/api/reset")
async def reset_simulation(req: ResetRequest):
    global session
    if session:
        await session.stop()
    session = SimSession(seed=req.seed, num_users=req.num_users, num_merchants=req.num_merchants)
    session.start()
    return {"status": "ok"}


@app.delete("/api/branches/{branch_id}")
async def delete_branch(branch_id: str):
    try:
        _session().delete_branch(branch_id)
        return {"status": "ok"}
    except KeyError:
        raise HTTPException(404, "Unknown branch") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/checkpoints/{checkpoint_id}/export-for-redteam")
async def export_for_redteam(checkpoint_id: str):
    """Bridge a demo (in-memory) checkpoint's state into the real Postgres
    ChronoDAG, returning a checkpoint_id the red-team harness can actually
    fork from.

    The demo simulation (api/sim_session.py) and red-team sessions
    (agents/redteam/harness.py) are separate composition roots — demo
    checkpoints live only in the in-process LiveChronoDAG and were never
    written to FINSIM_DB_URL, so handing their checkpoint_id straight to the
    harness's `PostgresChronoDAG.fork()` fails with "Checkpoint not found".
    This materializes an equivalent checkpoint the harness can see.
    """
    try:
        checkpoint_id = await asyncio.to_thread(_export_checkpoint_to_postgres, checkpoint_id)
    except KeyError:
        raise HTTPException(404, "Unknown checkpoint") from None
    except Exception as e:
        raise HTTPException(500, f"Failed to export to red-team store: {e}") from e
    return {"checkpoint_id": checkpoint_id}


def _export_checkpoint_to_postgres(checkpoint_id: str) -> str:
    import uuid

    engine = _session().build_engine_from_checkpoint(checkpoint_id)

    chrono = _get_redteam_chrono()
    if chrono is None:
        raise RuntimeError("FINSIM_DB_URL is not set — red-team sessions require it")

    checkpoint = chrono.import_branch_snapshot(
        branch_id=f"demo-export/{uuid.uuid4().hex[:8]}",
        event_number=engine._seq_num,
        sim_time_ns=engine.sim_time_ns,
        state_hash=engine.get_state_hash(),
        aggregate_snapshot=engine.get_full_snapshot_bytes(),
        rng_state=engine._rng.get_state(),
        metadata={"source": "demo_bridge", "origin_checkpoint": checkpoint_id, "server_run_id": SERVER_RUN_ID},
    )
    return checkpoint.checkpoint_id


class RedTeamStartRequest(BaseModel):
    from_genesis: bool = True
    checkpoint_id: str | None = None
    seed: int = 42
    use_graph: bool = False
    # Additional red-team branches whose target_notes/commit_reasoning get
    # pooled into this session's starting knowledge, on top of whichever
    # branch checkpoint_id itself was forked from — lets several
    # independent sessions' findings combine into one continuing session
    # (docs/redteam_agent_design.md §11).
    pool_from_branch_ids: list[str] = []


@app.post("/api/redteam/sessions")
async def start_redteam_session(req: RedTeamStartRequest):
    if not req.from_genesis and not req.checkpoint_id:
        raise HTTPException(400, "Pass checkpoint_id or set from_genesis")
    session_id = await redteam_observer.start_session(
        from_genesis=req.from_genesis, checkpoint_id=req.checkpoint_id,
        seed=req.seed, use_graph=req.use_graph, pool_from_branch_ids=req.pool_from_branch_ids,
    )
    return {"session_id": session_id}


@app.get("/api/redteam/sessions")
async def list_redteam_sessions():
    return [dataclasses.asdict(s) for s in redteam_observer.list_sessions()]


@app.get("/api/redteam/sessions/{session_id}")
async def get_redteam_session(session_id: str):
    try:
        return dataclasses.asdict(redteam_observer.get_session(session_id))
    except KeyError:
        raise HTTPException(404, "Unknown session") from None


@app.websocket("/api/redteam/stream/{session_id}")
async def redteam_stream(ws: WebSocket, session_id: str):
    await ws.accept()
    q = redteam_observer.subscribe(session_id)
    try:
        # Replay what's already happened before this subscriber connected —
        # steps that fired before the WebSocket handshake completed would
        # otherwise be silently missed (a live-only stream, unlike
        # /api/stream, has no periodic "tick" to eventually catch up on).
        try:
            existing = redteam_observer.get_session(session_id)
        except KeyError:
            await ws.send_json({"type": "error", "message": f"Unknown session {session_id}"})
            return

        for entry in existing.step_log:
            await ws.send_json({"type": "step", **entry})
        if existing.status != "running":
            # Was missing end_checkpoint_id/commit_reasoning — a client
            # that reconnects to (or clicks on, well after it finished) an
            # already-done session got this "done" message immediately,
            # and its `undefined` fields clobbered whatever the initial
            # GET /api/redteam/sessions/{id} fetch had correctly populated
            # (frontend's ws.onmessage spreads this over the previous
            # state). The "Continue from here" button and the commit
            # summary would silently disappear for any session you didn't
            # happen to be watching live when it finished.
            await ws.send_json({
                "type": "done", "status": existing.status,
                "committed": existing.committed, "error": existing.error,
                "end_checkpoint_id": existing.end_checkpoint_id,
                "commit_reasoning": existing.commit_reasoning,
            })
            return

        while True:
            message = await q.get()
            await ws.send_json(message)
            if message["type"] == "done":
                break
    except WebSocketDisconnect:
        pass
    finally:
        redteam_observer.unsubscribe(session_id, q)


@app.websocket("/api/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    s = _session()
    q = s.dag.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=1.0)
                await ws.send_json({
                    "type": "event",
                    "event": {
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "sim_time_ns": event.sim_time_ns,
                        "actor_id": event.actor_id,
                        "branch_id": event.branch_id,
                        "seq_num": event.seq_num,
                        "payload": event.payload,
                        "causation_id": event.causation_id,
                        "correlation_id": event.correlation_id,
                    },
                })
            except TimeoutError:
                await ws.send_json({"type": "tick", "state": s.branch_summary("main")})
    except WebSocketDisconnect:
        pass
    finally:
        s.dag.unsubscribe(q)
