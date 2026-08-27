"""FastAPI layer for the FinSim frontend demo.

Sits behind nothing but the `sim.*` public surfaces (engine, population,
scheduler) plus the local `api.live_dag` / `api.sim_session` helpers — see
those modules' docstrings for why this exists outside `sim/` rather than as
a sixth subsystem.

Run with:  uv run uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.sim_session import SimSession

session: SimSession | None = None


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
def health():
    return {"ok": True}


@app.get("/api/branches")
def list_branches():
    return _session().list_branch_graph()


@app.get("/api/branches/{branch_id}/state")
def branch_state(branch_id: str):
    try:
        return _session().branch_summary(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}")


@app.get("/api/branches/{branch_id}/accounts")
def branch_accounts(branch_id: str):
    try:
        return _session().accounts(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}")


@app.get("/api/branches/{branch_id}/accounts/{account_id}/events")
def account_events(branch_id: str, account_id: str):
    try:
        return _session().account_events(branch_id, account_id)
    except KeyError:
        raise HTTPException(404, "Unknown branch or account")


@app.get("/api/branches/{branch_id}/events")
def branch_events(branch_id: str, since_seq: int = 0, limit: int = 200):
    try:
        return _session().recent_events(branch_id, since_seq, limit)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}")


@app.get("/api/branches/{branch_id}/breakdown")
def branch_breakdown(branch_id: str):
    try:
        return _session().event_breakdown(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}")


@app.get("/api/branches/{branch_id}/checkpoints")
def branch_checkpoints(branch_id: str):
    try:
        return _session().checkpoints(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}")


@app.post("/api/branches/{branch_id}/checkpoint")
def make_checkpoint(branch_id: str):
    try:
        return _session().create_checkpoint(branch_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch {branch_id}")


class ForkRequest(BaseModel):
    parent_branch_id: str = "main"
    name: str
    checkpoint_id: str | None = None  # omit to fork from the current live head


@app.post("/api/branches/fork")
def fork_branch(req: ForkRequest):
    try:
        return _session().fork(req.parent_branch_id, req.name, req.checkpoint_id)
    except KeyError:
        raise HTTPException(404, f"Unknown branch or checkpoint")


class ChaosRequest(BaseModel):
    action: str
    params: dict = {}


@app.post("/api/branches/{branch_id}/chaos")
def chaos(branch_id: str, req: ChaosRequest):
    try:
        return _session().apply_chaos(branch_id, req.action, req.params)
    except KeyError as e:
        raise HTTPException(404, f"Unknown branch/account: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/diff")
def diff(branch_a: str, branch_b: str):
    try:
        return _session().diff(branch_a, branch_b)
    except KeyError:
        raise HTTPException(404, "Unknown branch")


class PauseRequest(BaseModel):
    paused: bool


@app.post("/api/pause")
def pause(req: PauseRequest):
    _session().set_paused(req.paused)
    return {"paused": req.paused}


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
            except asyncio.TimeoutError:
                await ws.send_json({"type": "tick", "state": s.branch_summary("main")})
    except WebSocketDisconnect:
        pass
    finally:
        s.dag.unsubscribe(q)
