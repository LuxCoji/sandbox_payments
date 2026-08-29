"""Drives one live simulation and manages ChronoDAG branches for the API.

Design:
  - "main" is the only auto-running branch: a background asyncio task calls
    `SimulationEnv.step()` in a loop, pacing itself so the event feed reads
    as "live" instead of dumping years of history in one tick.
  - Forking a branch takes a checkpoint of the *current* engine's aggregate
    state and clones it into a brand-new WorldEngineImpl on the new branch
    id. Forked branches are paused — no population loop of their own — so
    they behave as a sandbox: nothing changes until you act on them via
    `apply_chaos()`, which is exactly the point (freeze an account, shove
    money into one, then diff against main).

This module is the API's composition root — it plays the same role
`sim/main.py::build_simulation` plays for the CLI, just reusing the same
`sim.*` subsystems rather than a concrete module no one else should import.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import uuid
from collections import Counter
from dataclasses import dataclass

from api.live_dag import LiveChronoDAG
from sim.core.engine import WorldEngineImpl
from sim.core.events import AccountCredited, AccountDebited, AccountFrozen, AccountStatusChanged
from sim.core.interfaces import AccountStatus, Command, TransactionType
from sim.population.agents import PopulationManager
from sim.population.behaviour import PopulationBehaviourModel
from sim.population.calibration import calibrate_from_csv
from sim.population.interfaces import CalibratedParams
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG

STEP_BATCH = 12          # events processed per tick
TICK_SECONDS = 0.12      # pacing — keeps the feed watchable, not a firehose
AUTO_CHECKPOINT_INTERVAL = 180  # events between automatic checkpoints on "main"


@dataclass
class BranchHandle:
    branch_id: str
    name: str
    engine: WorldEngineImpl
    live: bool  # True only for "main" — has an auto-stepping population loop


class SimSession:
    def __init__(self, seed: int = 42, num_users: int = 60, num_merchants: int = 8) -> None:
        self.dag = LiveChronoDAG()
        self.branches: dict[str, BranchHandle] = {}
        self._task: asyncio.Task | None = None
        self._seed = seed
        self._num_users = num_users
        self._num_merchants = num_merchants
        self._paused = False
        # checkpoint_id -> deep-copied aggregate state at that instant, so a
        # fork can rebuild an engine from ANY past checkpoint (real time
        # travel), not just the current live head.
        self._checkpoint_snapshots: dict[str, dict] = {}
        self._last_auto_checkpoint_seq = 0

        rng = DeterministicRNG.from_seed(seed)
        env = SimulationEnv()
        engine = WorldEngineImpl(env=env, rng=rng, branch_id="main", chrono=self.dag)

        data_dir = os.path.join(os.path.dirname(__file__), "..", "data", "paysim")
        try:
            params = calibrate_from_csv(data_dir)
        except FileNotFoundError:
            params = CalibratedParams({}, (), {}, {}, {})

        behaviour_model = PopulationBehaviourModel(params, engine._rng)
        population = PopulationManager(behaviour_model, engine._rng)
        population.create_population(num_users=num_users, num_merchants=num_merchants, engine=engine)
        population.start_agent_loops(engine)

        self.branches["main"] = BranchHandle("main", "main", engine, live=True)
        self._population = population

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run_loop(self) -> None:
        # asyncio silently drops exceptions from a task no one awaits — log
        # explicitly so a bug in a stepped event handler doesn't just freeze
        # "main" with no trace of why.
        import logging
        logger = logging.getLogger("finsim.api")
        engine = self.branches["main"].engine
        while True:
            if not self._paused:
                try:
                    for _ in range(STEP_BATCH):
                        processed = engine._env.step()
                        if processed is None:
                            break
                    if engine._seq_num - self._last_auto_checkpoint_seq >= AUTO_CHECKPOINT_INTERVAL:
                        # Offload to a thread to avoid blocking the event loop with copy.deepcopy
                        await asyncio.to_thread(self.create_checkpoint, "main")
                        self._last_auto_checkpoint_seq = engine._seq_num
                except Exception:
                    logger.exception("main branch step loop crashed; pausing")
                    self._paused = True
            await asyncio.sleep(TICK_SECONDS)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    # ── branch inspection ───────────────────────────────────────────

    def branch_summary(self, branch_id: str) -> dict:
        handle = self.branches[branch_id]
        engine = handle.engine
        accounts = list(engine._accounts.values())
        money_supply = sum(a.balance_paise for a in accounts)
        row = self.dag.get_branch_row(branch_id)
        return {
            "branch_id": branch_id,
            "name": handle.name,
            "live": handle.live,
            "sim_time_ns": engine.sim_time_ns,
            "state_hash": engine.get_state_hash(),
            "money_supply_paise": money_supply,
            "account_count": len(accounts),
            "tx_count": len(engine._payments),
            "step_count": engine._env.step_count,
            "head_seq_num": row.branch.head_seq_num,
            "parent_branch_id": row.branch.parent_branch_id,
            "parent_checkpoint_id": row.branch.parent_checkpoint_id,
        }

    def list_branch_graph(self) -> list[dict]:
        out = []
        for row in self.dag.list_branches():
            b = row.branch
            handle = self.branches.get(b.branch_id)
            out.append({
                "branch_id": b.branch_id,
                "name": row.name,
                "parent_branch_id": b.parent_branch_id,
                "parent_checkpoint_id": b.parent_checkpoint_id,
                "fork_seq_num": (
                    self.dag._checkpoints_by_id[b.parent_checkpoint_id].event_number
                    if b.parent_checkpoint_id else 0
                ),
                "head_seq_num": b.head_seq_num,
                "live": handle.live if handle else False,
                "seed_offset": b.seed_offset,
                "created_at_ns": b.created_at_ns,
                "checkpoint_seq_nums": sorted(row.checkpoints.keys()),
            })
        return out

    def accounts(self, branch_id: str) -> list[dict]:
        engine = self.branches[branch_id].engine
        # BANK_OPS masks owner_id — for the demo inspector we want real ids,
        # so re-derive snapshots straight from aggregates instead of going
        # through get_world_view().
        out = []
        for acc in engine._accounts.values():
            snap = acc.to_snapshot()
            out.append({
                "account_id": snap.account_id,
                "account_type": snap.account_type.value,
                "balance_paise": snap.balance_paise,
                "status": snap.status.value,
                "kyc_level": snap.kyc_level,
                "owner_id": snap.owner_id,
                "daily_tx_count": snap.daily_tx_count,
                "daily_tx_volume_paise": snap.daily_tx_volume_paise,
                "merchant_category_code": snap.merchant_category_code,
            })
        return out

    def account_events(self, branch_id: str, account_id: str, limit: int = 30) -> list[dict]:
        row = self.dag.get_branch_row(branch_id)
        events = [
            e for e in row.events
            if e.payload.get("account_id") == account_id
            or e.payload.get("source_account_id") == account_id
            or e.payload.get("destination_account_id") == account_id
        ]
        return [_serialize_event(e) for e in events[-limit:]]

    def recent_events(self, branch_id: str, since_seq: int = 0, limit: int = 200) -> list[dict]:
        return [_serialize_event(e) for e in self.dag.list_events(branch_id, since_seq, limit)]

    def event_breakdown(self, branch_id: str) -> dict[str, int]:
        row = self.dag.get_branch_row(branch_id)
        counts = Counter(e.event_type for e in row.events)
        return dict(counts.most_common())

    # ── checkpoints ─────────────────────────────────────────────────

    def _snapshot_engine_state(self, engine: WorldEngineImpl) -> dict:
        return {
            "accounts": copy.deepcopy(engine._accounts),
            "payments": copy.deepcopy(engine._payments),
            "devices": copy.deepcopy(engine._devices),
            "merchants": copy.deepcopy(engine._merchants),
            "gateways": copy.deepcopy(engine._gateways),
            "seq_num": engine._seq_num,
            "tx_counter": engine._tx_counter,
            # dict[str, None], not a set — WorldEngineImpl uses it as
            # `self._processed_idempotency_keys[key] = result`
            # (execute_command's idempotency cache) — a set doesn't support
            # item assignment. This snapshot has to preserve that shape
            # exactly, since a checkpoint built here can now feed back into
            # a real WorldEngineImpl via the "export for red team" bridge
            # (api/main.py::_export_checkpoint_to_postgres ->
            # engine.get_full_snapshot_bytes() -> restore_full_snapshot_bytes()
            # on the red-team branch), where a wrong type here surfaces as
            # a genuine TypeError crash three hops away from this line.
            "idempotency_keys": dict(engine._processed_idempotency_keys),
        }

    def create_checkpoint(self, branch_id: str) -> dict:
        """Snapshot a branch's current aggregate state. Auto-called every
        `AUTO_CHECKPOINT_INTERVAL` events on main; also callable manually
        (used by the UI's "checkpoint now" action and by fork())."""
        engine = self.branches[branch_id].engine
        cp = self.dag.create_checkpoint(
            branch_id=branch_id,
            event_number=engine._seq_num,
            sim_time_ns=engine.sim_time_ns,
            state_hash=engine.get_state_hash(),
            aggregate_snapshot=engine.get_canonical_state_bytes(),
            rng_state=engine._rng.get_state(),
        )
        self._checkpoint_snapshots[cp.checkpoint_id] = self._snapshot_engine_state(engine)
        return {
            "checkpoint_id": cp.checkpoint_id,
            "branch_id": cp.branch_id,
            "event_number": cp.event_number,
            "sim_time_ns": cp.sim_time_ns,
            "state_hash": cp.state_hash,
        }

    def checkpoints(self, branch_id: str) -> list[dict]:
        row = self.dag.get_branch_row(branch_id)
        out = []
        for num in sorted(row.checkpoints):
            cp = row.checkpoints[num]
            out.append({
                "checkpoint_id": cp.checkpoint_id,
                "branch_id": cp.branch_id,
                "event_number": cp.event_number,
                "sim_time_ns": cp.sim_time_ns,
                "state_hash": cp.state_hash,
                "has_snapshot": cp.checkpoint_id in self._checkpoint_snapshots,
            })
        return out

    def build_engine_from_checkpoint(self, checkpoint_id: str) -> WorldEngineImpl:
        """Rebuild a standalone engine from a *specific* past checkpoint's
        stored snapshot — the same reconstruction `fork()` does, but without
        registering a new demo branch. Used by the "export to red-team
        store" bridge (api/main.py) so exporting an old checkpoint reflects
        that checkpoint's state, not whatever the branch has moved on to
        since.
        """
        if checkpoint_id not in self._checkpoint_snapshots:
            raise KeyError(checkpoint_id)
        snapshot = self._checkpoint_snapshots[checkpoint_id]
        cp = self.dag._checkpoints_by_id[checkpoint_id]

        env = SimulationEnv(start_time_ns=int(cp.sim_time_ns))
        rng = DeterministicRNG.from_seed(self._seed)  # deterministic re-derivation, same caveat as fork()
        engine = WorldEngineImpl(env=env, rng=rng, branch_id="export-tmp", chrono=None)
        engine._accounts = copy.deepcopy(snapshot["accounts"])
        engine._payments = copy.deepcopy(snapshot["payments"])
        engine._devices = copy.deepcopy(snapshot["devices"])
        engine._merchants = copy.deepcopy(snapshot["merchants"])
        engine._gateways = copy.deepcopy(snapshot["gateways"])
        engine._seq_num = snapshot["seq_num"]
        engine._tx_counter = snapshot["tx_counter"]
        engine._processed_idempotency_keys = dict(snapshot["idempotency_keys"])  # see _snapshot_engine_state
        return engine

    # ── forking (real time travel: fork from ANY stored checkpoint) ─

    def fork(self, parent_branch_id: str, name: str, checkpoint_id: str | None = None) -> dict:
        if checkpoint_id is None:
            # No checkpoint specified — snapshot the current live head first.
            checkpoint_id = self.create_checkpoint(parent_branch_id)["checkpoint_id"]
        elif checkpoint_id not in self._checkpoint_snapshots:
            raise KeyError(checkpoint_id)

        snapshot = self._checkpoint_snapshots[checkpoint_id]
        new_branch_id = f"br-{uuid.uuid4().hex[:8]}"
        self.dag.fork(checkpoint_id, new_branch_id, metadata={"name": name})

        cloned_env = SimulationEnv()
        cloned_rng = DeterministicRNG.from_seed(self._seed)  # deterministic re-derivation
        cloned_engine = WorldEngineImpl(
            env=cloned_env, rng=cloned_rng, branch_id=new_branch_id, chrono=self.dag,
        )
        # Rebuild aggregate state from the checkpoint's own snapshot — this
        # is what makes forking from a *past* checkpoint real time travel
        # rather than always branching off the live head.
        cloned_engine._accounts = copy.deepcopy(snapshot["accounts"])
        cloned_engine._payments = copy.deepcopy(snapshot["payments"])
        cloned_engine._devices = copy.deepcopy(snapshot["devices"])
        cloned_engine._merchants = copy.deepcopy(snapshot["merchants"])
        cloned_engine._gateways = copy.deepcopy(snapshot["gateways"])
        cloned_engine._seq_num = snapshot["seq_num"]
        cloned_engine._tx_counter = snapshot["tx_counter"]
        cloned_engine._processed_idempotency_keys = dict(snapshot["idempotency_keys"])  # see _snapshot_engine_state

        self.branches[new_branch_id] = BranchHandle(new_branch_id, name, cloned_engine, live=False)
        return self.branch_summary(new_branch_id)

    def delete_branch(self, branch_id: str) -> None:
        if branch_id not in self.branches:
            raise KeyError(branch_id)

        self.dag.delete_branch(branch_id)
        self.branches.pop(branch_id)

        to_delete = [cp_id for cp_id in list(self._checkpoint_snapshots.keys())
                     if cp_id.startswith(f"cp-{branch_id}-")]
        for cp_id in to_delete:
            self._checkpoint_snapshots.pop(cp_id, None)

    # ── chaos / sandbox actions (forked branches only) ─────────────

    def apply_chaos(self, branch_id: str, action: str, params: dict) -> dict:
        handle = self.branches[branch_id]
        if handle.live:
            raise ValueError("Chaos actions are only allowed on forked (paused) branches, not 'main'.")
        engine = handle.engine

        if action == "freeze_account":
            event = engine._create_event(
                AccountFrozen, actor_id="sandbox-admin", account_id=params["account_id"],
            )
            engine._persist_events([event])
            engine._apply_events([event])
        elif action == "unfreeze_account":
            acc = engine._accounts[params["account_id"]]
            event = engine._create_event(
                AccountStatusChanged, actor_id="sandbox-admin", account_id=params["account_id"],
                old_status=acc.status, new_status=AccountStatus.ACTIVE
            )
            engine._persist_events([event])
            engine._apply_events([event])
        elif action == "override_balance":
            account_id = params["account_id"]
            target_paise = int(params["balance_paise"])
            acc = engine._accounts[account_id]
            delta = target_paise - acc.balance_paise
            if delta >= 0:
                event = engine._create_event(
                    AccountCredited, actor_id="sandbox-admin", account_id=account_id,
                    amount_paise=delta, tx_id=f"chaos-{uuid.uuid4().hex[:8]}",
                )
            else:
                event = engine._create_event(
                    AccountDebited, actor_id="sandbox-admin", account_id=account_id,
                    amount_paise=-delta, tx_id=f"chaos-{uuid.uuid4().hex[:8]}",
                )
            engine._persist_events([event])
            engine._apply_events([event])
        elif action == "transfer":
            cmd = Command(
                command_id=str(uuid.uuid4()),
                actor_id=params.get("actor_id", "sandbox-admin"),
                action_type=TransactionType.TRANSFER,
                source_account_id=params["source_account_id"],
                target_account_id=params["target_account_id"],
                amount_paise=int(params["amount_paise"]),
                idempotency_key=str(uuid.uuid4()),
            )
            engine.execute_command(cmd)
        else:
            raise ValueError(f"Unknown chaos action: {action}")

        return self.branch_summary(branch_id)

    def diff(self, branch_a: str, branch_b: str) -> dict:
        seq_a = self.branches[branch_a].engine._seq_num
        seq_b = self.branches[branch_b].engine._seq_num
        at_event = min(seq_a, seq_b)
        # diff() requires checkpoints at the *same* event_number on both
        # branches — create them here rather than assuming one exists.
        for bid in (branch_a, branch_b):
            eng = self.branches[bid].engine
            self.dag.create_checkpoint(
                branch_id=bid, event_number=at_event, sim_time_ns=eng.sim_time_ns,
                state_hash=eng.get_state_hash(), aggregate_snapshot=eng.get_canonical_state_bytes(), rng_state=b"",
            )
        d = self.dag.diff(branch_a, branch_b, at_event)
        return {
            "branch_a_id": d.branch_a_id,
            "branch_b_id": d.branch_b_id,
            "at_event": d.at_event,
            "events_only_in_a": d.events_only_in_a,
            "events_only_in_b": d.events_only_in_b,
            "added": [{"entity_type": e.entity_type, "entity_id": e.entity_id} for e in d.entities_added],
            "modified": [{"entity_type": e.entity_type, "entity_id": e.entity_id} for e in d.entities_modified],
        }


def _serialize_event(e) -> dict:
    return {
        "event_id": e.event_id,
        "event_type": e.event_type,
        "sim_time_ns": e.sim_time_ns,
        "actor_id": e.actor_id,
        "branch_id": e.branch_id,
        "seq_num": e.seq_num,
        "payload": e.payload,
        "causation_id": e.causation_id,
        "correlation_id": e.correlation_id,
    }
