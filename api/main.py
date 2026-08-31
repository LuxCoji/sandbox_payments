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
from pathlib import Path
import time
SERVER_RUN_ID = uuid.uuid4().hex

import dataclasses
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.redteam_session import RedTeamObserver
from api.sim_session import SimSession
from sim.config import SimConfig

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


SERVER_START_TIME_WALL = time.time()

def _list_redteam_branches() -> list[dict]:
    chrono = _get_redteam_chrono()
    if chrono is None:
        return []

    with chrono.conn.cursor() as cur:
        # Join with checkpoints to find the exact event number this branch was forked from,
        # and the metadata to see which demo checkpoint it originated from.
        cur.execute(
            '''SELECT b.branch_id, b.head_seq_num, b.created_at_ns, c.event_number, c.metadata, c.branch_id, b.metadata
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

    out = []
    for branch_id, head_seq_num, created_at_ns, fork_seq_num, c_meta, c_branch_id, b_meta in rows:
        if not b_meta or b_meta.get("created_at_wall", 0) < SERVER_START_TIME_WALL:
            continue

        parent_branch_id = None
        if c_branch_id:
            if c_branch_id.startswith("demo-export/"):
                if c_meta and c_meta.get("server_run_id") == SERVER_RUN_ID:
                    origin_cp_id = c_meta.get("origin_checkpoint")
                    if origin_cp_id in _session().dag._checkpoints_by_id:
                        parent_branch_id = _session().dag._checkpoints_by_id[origin_cp_id].branch_id
            else:
                parent_branch_id = c_branch_id

        out.append({
            "branch_id": branch_id,
            "name": f"🔴 {branch_id.removeprefix('red-team/')}",
            "parent_branch_id": parent_branch_id,
            "parent_checkpoint_id": None,
            "fork_seq_num": fork_seq_num or 0,
            "head_seq_num": head_seq_num,
            "live": False,  # forked branches never auto-run — static except for agent actions
            "seed_offset": 0,
            "created_at_ns": created_at_ns,
            "checkpoint_seq_nums": sorted(checkpoints_by_branch.get(branch_id, [])),
            "commit_reasoning": c_meta.get("commit_reasoning", "No reasoning recorded") if c_meta else "No reasoning recorded",
            "pool_from_branch_ids": c_meta.get("pool_from_branch_ids", []) if c_meta else [],
        })
    return out


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


# ── Fraud detection ───────────────────────────────────────────────────────
#
# The rails watch the live branch and expose two things: what they have seen,
# and what they have flagged. Read-only here - a freeze is a separate action
# with a named reviewer behind it, and putting it on an unauthenticated GET
# would make it a one-click way to freeze someone's account.


@app.get("/api/risk/summary")
async def risk_summary():
    """Counts, flag rate, and whether a trained card model is actually loaded."""
    return _session().risk_summary()


@app.get("/api/risk/cases")
async def risk_cases(limit: int = 100):
    """Open cases, newest first. Each carries the evidence that raised it."""
    return {"cases": _session().risk_cases(limit=limit)}


@app.get("/api/risk/console", response_class=HTMLResponse)
async def risk_console():
    """The whole queue as a page, for a reviewer who wants to read rather than poll."""
    from risk.console import render

    session = _session()
    summary = session.risk_summary()
    if not summary.get("enabled"):
        return HTMLResponse("<p>Fraud detection is not enabled for this session.</p>")

    # `cases` is a bounded deque, which does not slice.
    cases = list(session.risk.cases)[-200:][::-1] if session.risk else []
    return HTMLResponse(render(
        summary, cases, card_model_loaded=summary.get("card_model_loaded", False),
        run_label="live session"))


# ── Retraining ────────────────────────────────────────────────────────────
#
# The button on the dashboard calls this. It does **not** make the model learn
# continuously from live traffic - see `risk/registry.py` for why that is the
# wrong thing to build. It trains a candidate, scores it and the live model on
# the same held-out period, and promotes only if the candidate wins.
#
# Training takes minutes, so the endpoint starts the work and returns. The
# dashboard polls for the outcome rather than holding a request open.

_retrain_state: dict = {"status": "idle", "result": None, "error": None}


def _run_retrain(traffic: Path, models: Path) -> None:
    from risk.retrain import NotEnoughData, retrain

    try:
        _retrain_state["result"] = retrain(traffic, models)
        _retrain_state["status"] = "done"
    except NotEnoughData as exc:
        # Not an error. Declining to retrain on a sample too small to measure
        # is the system working, and the dashboard says so in those words.
        _retrain_state["status"] = "declined"
        _retrain_state["error"] = str(exc)
    except Exception as exc:
        _retrain_state["status"] = "failed"
        _retrain_state["error"] = f"{type(exc).__name__}: {exc}"


@app.post("/api/risk/retrain")
async def start_retrain(background: BackgroundTasks):
    """Train a candidate from collected traffic and promote it if it wins."""
    if _retrain_state["status"] == "running":
        raise HTTPException(409, "a retrain is already running")

    config = SimConfig()
    traffic = config.traffic_log or Path("runs/traffic.jsonl")
    if not Path(traffic).exists():
        raise HTTPException(
            400, f"no collected traffic at {traffic}. Set FINSIM_TRAFFIC_LOG "
                 f"and run the simulation - a model cannot be retrained on "
                 f"traffic nobody recorded.")

    _retrain_state.update(status="running", result=None, error=None)
    background.add_task(_run_retrain, Path(traffic),
                        Path(config.card_model_path).parent)
    return {"status": "running"}


@app.get("/api/risk/retrain")
async def retrain_status():
    """Where the last retrain got to, and what it decided."""
    from risk.registry import Registry

    config = SimConfig()
    registry = Registry.load(Path(config.card_model_path).parent)
    return {**_retrain_state, "registry": registry.summary()}


@app.post("/api/risk/rollback")
async def rollback_model():
    """Put the previous promoted model back.

    A promotion that looked right on a holdout can still be wrong in
    production - a holdout is a period, not the future.
    """
    from risk.registry import Registry

    config = SimConfig()
    registry = Registry.load(Path(config.card_model_path).parent)
    previous = registry.rollback()
    if previous is None:
        raise HTTPException(400, "no earlier promoted model to roll back to")
    return {"rolled_back_to": previous.to_dict(),
            "note": "restart the session to load it"}


# ── Acting on a case ──────────────────────────────────────────────────────
#
# A flagged case is a question, not a decision. These are the answers a
# reviewer can give, and each one is recorded with their name against it.
#
# **Nothing here freezes an account on a model's say-so.** The wire rail runs at
# roughly 12% precision, so an automatic freeze would stop about eight innocent
# parties per real laundering operation. `risk/actions.py` refuses a freeze with
# no named reviewer and requires a second above one crore.


class Decision(BaseModel):
    case_id: str
    reviewer: str
    reason: str = ""
    second_reviewer: str | None = None


def _case_log():
    from risk.actions import CaseLog

    return CaseLog(Path("runs/case_decisions.jsonl"))


def _find_case(case_id: str):
    session = _session()
    if session.risk is None:
        raise HTTPException(400, "fraud detection is not enabled")
    for case in session.risk.cases:
        if str(case.tx_id) == case_id:
            return case
    raise HTTPException(404, f"no open case {case_id}")


@app.post("/api/risk/cases/freeze")
async def freeze_case(decision: Decision):
    """Request a freeze on the accounts in a case.

    Records an intent and returns it. **It does not freeze anything** - the
    engine holds the funds and should act on an instruction with a case behind
    it, not on a callback from a model.
    """
    from risk.actions import request_freeze

    try:
        recorded = request_freeze(
            _find_case(decision.case_id), reviewer=decision.reviewer,
            reason=decision.reason, log=_case_log(),
            second_reviewer=decision.second_reviewer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return recorded.to_dict()


@app.post("/api/risk/cases/clear")
async def clear_case_endpoint(decision: Decision):
    """Record that a reviewer judged a case not to be fraud.

    The case is not deleted. The rail flagged it for a stated reason, and a
    reviewer's judgement is one piece of evidence rather than the last word.
    """
    from risk.actions import clear_case

    try:
        recorded = clear_case(_find_case(decision.case_id),
                              reviewer=decision.reviewer,
                              reason=decision.reason, log=_case_log())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return recorded.to_dict()


@app.post("/api/risk/cases/step-up")
async def step_up_case(decision: Decision):
    """Challenge the customer - an OTP or equivalent. Card rail only.

    On the wire rail this is refused rather than ignored. Telling someone they
    are under money-laundering review is tipping off, a criminal offence, and a
    challenge saying "confirm this payment" is exactly that message.
    """
    from risk.actions import notify_customer, request_information

    case = _find_case(decision.case_id)
    permitted = notify_customer(case.rail, "STEP_UP")
    if not permitted["notify"]:
        raise HTTPException(400, permitted["reason"])

    recorded = request_information(case, reviewer=decision.reviewer,
                                   question=decision.reason or "step-up sent",
                                   log=_case_log())
    return {**recorded.to_dict(), "customer_message": permitted["message"]}


@app.get("/api/risk/decisions")
async def list_decisions():
    """Every decision taken, and what they add up to."""
    from risk.actions import summarise

    log = _case_log()
    return {"decisions": [d.to_dict() for d in log.all()[-50:]][::-1],
            "summary": summarise(log)}


# ── Serving the dashboard ─────────────────────────────────────────────────
#
# Mounted last, and only when a build exists. The frontend calls `/api/...` on
# its own origin, so serving it from this process means one URL and no CORS
# rules to keep in step with a second service.
#
# Every route above is already registered, so the catch-all below cannot shadow
# one - but an unknown `/api/...` path must still 404 rather than fall through
# to index.html, or a typo'd endpoint returns a page and reads as a frontend bug.

_STATIC = Path(__file__).resolve().parents[1] / "static"

if _STATIC.is_dir():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=_STATIC / "assets"), name="assets")

    @app.get("/{path:path}")
    async def dashboard(path: str):
        """The single-page app, and its client-side routes."""
        if path.startswith("api/"):
            raise HTTPException(404, "no such endpoint")
        candidate = _STATIC / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_STATIC / "index.html")
