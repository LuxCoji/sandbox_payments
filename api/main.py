"""FastAPI layer for the FinSim frontend demo.

Sits behind nothing but the `sim.*` public surfaces (engine, population,
scheduler) plus the local `api.live_dag` / `api.sim_session` helpers — see
those modules' docstrings for why this exists outside `sim/` rather than as
a sixth subsystem.

Run with:  uv run uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.redteam_session import RedTeamObserver
from api.sim_session import SimSession

session: SimSession | None = None
redteam_observer = RedTeamObserver()


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
    return _session().list_branch_graph()


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


class RedTeamStartRequest(BaseModel):
    from_genesis: bool = True
    checkpoint_id: str | None = None
    seed: int = 42
    use_graph: bool = False


@app.post("/api/redteam/sessions")
async def start_redteam_session(req: RedTeamStartRequest):
    if not req.from_genesis and not req.checkpoint_id:
        raise HTTPException(400, "Pass checkpoint_id or set from_genesis")
    session_id = await redteam_observer.start_session(
        from_genesis=req.from_genesis, checkpoint_id=req.checkpoint_id,
        seed=req.seed, use_graph=req.use_graph,
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
            await ws.send_json({
                "type": "done", "status": existing.status,
                "committed": existing.committed, "error": existing.error,
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
