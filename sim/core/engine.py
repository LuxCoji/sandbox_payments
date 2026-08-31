"""WorldEngine implementation — the core simulation engine.

Owns the SimulationEnv and all in-memory aggregate projections.
Mutates state strictly via domain events through apply_event pipeline.

Follows the plan's CQRS pipeline: Command -> Validate -> Emit Event(s) ->
Append to ChronoDAG -> Apply to in-memory aggregates. The ChronoDAG
dependency is optional (None persists nothing, useful for isolated unit
tests) but is always wired in the real composition root (sim/main.py).
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import pickle
import time
import uuid
from typing import TYPE_CHECKING, Any

from sim.chrono.interfaces import ChronoDAG, StoredEvent
from sim.core.account import Account
from sim.core.device import Device
from sim.core.events import (
    AccountCreated,
    AccountCredited,
    AccountDebited,
    DailyCountersReset,
    DeviceRegistered,
    DomainEvent,
    FeeCharged,
    GatewayStatusChanged,
    InterestAccrued,
    MerchantOnboarded,
    PaymentAuthorized,
    PaymentChargedBack,
    PaymentDeclined,
    PaymentRequested,
    SettlementBatchCompleted,
    SettlementBatchCreated,
    TransferRejected,
)
from sim.core.gateway import GatewayEntity
from sim.core.interfaces import (
    AccountSnapshot,
    AccountStatus,
    AccountType,
    ActorRole,
    Command,
    CommandResult,
    GlobalParams,
    RiskAction,
    RiskContext,
    RiskDecision,
    RiskScorer,
    TransactionType,
    WorldView,
)
from sim.core.merchant import Merchant
from sim.core.payment import Payment
from sim.core.settlement import SettlementBatch
from sim.observability import EVENT_LATENCY, EVENTS_PROCESSED, SCHEDULER_QUEUE_SIZE, traced

if TYPE_CHECKING:
    from sim.scheduler.env import ScheduledEvent, SimulationEnv
    from sim.scheduler.rng import DeterministicRNG


class WorldEngineImpl:
    """Concrete implementation of the WorldEngine protocol."""

    def __init__(
        self,
        env: SimulationEnv,
        rng: DeterministicRNG,
        branch_id: str = "main",
        global_params: GlobalParams | None = None,
        chrono: ChronoDAG | None = None,
        seq_num: int = 0,
        risk: RiskScorer | None = None,
    ) -> None:
        self._env = env
        self._rng = rng
        self._branch_id = branch_id
        self._chrono = chrono
        self._seq_num: int = seq_num
        # Injected, never imported: sim must not depend on the risk package.
        # With None the engine emits exactly what it emitted before risk
        # scoring existed, so every replay and determinism test stays valid.
        self._risk = risk

        self._accounts: dict[str, Account] = {}
        self._payments: dict[str, Payment] = {}
        self._devices: dict[str, Device] = {}
        self._merchants: dict[str, Merchant] = {}
        self._gateways: dict[str, GatewayEntity] = {}
        self._settlement_batches: dict[str, SettlementBatch] = {}

        self._global_params = global_params or GlobalParams(
            fee_schedules=(), rail_limits=(), settlement_cut_off_ns=0.0
        )

        # Idempotency cache: execute_command() stores/returns the actual
        # CommandResult on a repeated idempotency_key, not None — the
        # annotation was wrong (mypy has been flagging both the return at
        # line ~180 and the assignment at ~221 all along); fixed here.
        self._processed_idempotency_keys: dict[str, CommandResult] = {}
        self._tx_counter: int = 0

    @property
    def sim_time_ns(self) -> float:
        return self._env.now

    def schedule_event(self, event: ScheduledEvent) -> None:
        self._env.schedule(event)
        SCHEDULER_QUEUE_SIZE.set(self._env.queue_size)

    def get_world_view(
        self, actor_id: str, actor_role: ActorRole, offset: int = 0, limit: int = 1000
    ) -> WorldView:
        merchants = tuple(m.to_directory_entry() for m in self._merchants.values())
        devices = tuple(d.to_snapshot() for d in self._devices.values() if d.owner_id == actor_id)

        visible_accounts: list[AccountSnapshot] = []
        if actor_role in (ActorRole.USER, ActorRole.MERCHANT):
            for acc in self._accounts.values():
                if acc.owner_id == actor_id:
                    visible_accounts.append(acc.to_snapshot())
        elif actor_role == ActorRole.RED_AGENT:
            # Deliberately white-box (docs/redteam_agent_design.md): a
            # red-team engagement gets more knowledge than a blind external
            # attacker would, on purpose — the point is to stress-test
            # detection coverage across the full attack surface, not to
            # model how a real fraudster discovers targets. Own accounts
            # are shown in full (owner_id == actor_id, nothing to mask);
            # every other account gets the same PII masking as
            # BANK_OPS/RISK_ANALYST/BLUE_AGENT below — real account_id/
            # balance/status/kyc (the agent needs those to pick and reason
            # about a target), pseudonymous owner_id (it doesn't need
            # anyone's real identity to transact with their account).
            #
            # Visibility here is deliberately broader than authorization:
            # seeing an account's id/balance lets the agent target it as a
            # transfer_funds/make_payment *destination*, but
            # _execute_transfer()/_execute_payment() separately enforce
            # that only an account's real owner can spend from it as the
            # *source* (reason_code UNAUTHORIZED_SOURCE). That check was
            # missing for one build of this harness — visibility into
            # another account's id was, briefly, the only thing standing
            # between an actor and draining it — and got closed once a
            # red-team session found and demonstrated exactly that gap.
            for acc in self._accounts.values():
                snap = acc.to_snapshot()
                if acc.owner_id == actor_id:
                    visible_accounts.append(snap)
                else:
                    visible_accounts.append(dataclasses.replace(
                        snap,
                        owner_id=self._mask_owner_id(snap.owner_id),
                        linked_device_ids=tuple(self._mask_owner_id(did) for did in snap.linked_device_ids)
                    ))
        elif actor_role in (ActorRole.BANK_OPS, ActorRole.RISK_ANALYST, ActorRole.BLUE_AGENT):
            # PII masking: cross-account-visibility roles see every account, but
            # the owner's real ID and device IDs are replaced with stable
            # pseudonymous hashes so no other actor's raw identity leaks through.
            for i, acc in enumerate(self._accounts.values()):
                if i < offset:
                    continue
                if len(visible_accounts) >= limit:
                    break
                snap = acc.to_snapshot()
                masked_snap = dataclasses.replace(
                    snap,
                    owner_id=self._mask_owner_id(snap.owner_id),
                    linked_device_ids=tuple(self._mask_owner_id(did) for did in snap.linked_device_ids)
                )
                visible_accounts.append(masked_snap)

        return WorldView(
            actor_id=actor_id,
            actor_role=actor_role,
            sim_time_ns=self.sim_time_ns,
            accounts=tuple(visible_accounts),
            merchants=merchants,
            devices=devices,
            global_params=self._global_params,
        )

    @traced("WorldEngine.execute_command")
    def execute_command(self, command: Command) -> CommandResult:
        if command.idempotency_key in self._processed_idempotency_keys:
            return self._processed_idempotency_keys[command.idempotency_key]

        handler_name = {
            TransactionType.PAYMENT: "_execute_payment",
            TransactionType.TRANSFER: "_execute_transfer",
            TransactionType.CASH_IN: "_execute_cash_in",
            TransactionType.CASH_OUT: "_execute_cash_out",
            TransactionType.DEBIT: "_execute_debit",
            TransactionType.REFUND: "_execute_refund",
            TransactionType.CHARGEBACK: "_execute_chargeback",
            TransactionType.SETTLEMENT: "_execute_settlement",
            TransactionType.FEE: "_execute_fee",
            TransactionType.INTEREST: "_execute_interest",
        }.get(command.action_type)

        if not handler_name:
            raise ValueError(f"Unsupported action type: {command.action_type}")

        events = []
        events.extend(self._get_daily_reset_events(
            [command.source_account_id or "", command.target_account_id or ""],
            command.actor_id
        ))

        handler = getattr(self, handler_name)
        events.extend(handler(command))

        if not events:
            # Fallback if no logic happened (shouldn't really happen)
            pass
        else:
            self._persist_events(events)
            self._apply_events(events)

        rejection_types = {
            "PaymentDeclined", "TransferRejected", "RefundRejected",
            "AccountFreezeFailed", "PaymentTimeout",
        }
        success = len(events) > 0 and all(type(e).__name__ not in rejection_types for e in events)
        result = CommandResult(events=tuple(events), success=success)

        self._processed_idempotency_keys[command.idempotency_key] = result
        if len(self._processed_idempotency_keys) > 10000:
            self._processed_idempotency_keys.pop(next(iter(self._processed_idempotency_keys)))

        return result

    def create_account(
        self,
        account_id: str,
        owner_id: str,
        account_type: AccountType,
        initial_balance_paise: int,
        kyc_level: int,
    ) -> None:
        """Genesis-create an account through the same Emit -> Append -> Apply
        pipeline as execute_command(), so population bootstrapping is
        persisted and replay-consistent like any other domain event."""
        event = self._create_event(
            AccountCreated, actor_id=owner_id, account_id=account_id,
            account_type=account_type, initial_balance_paise=initial_balance_paise,
            kyc_level=kyc_level, owner_id=owner_id,
        )
        self._persist_events([event])
        self._apply_events([event])

    def get_canonical_state_bytes(self) -> bytes:
        canonical = {
            "accounts": {k: v.to_canonical_dict() for k, v in sorted(self._accounts.items())},
            "devices": {k: v.to_canonical_dict() for k, v in sorted(self._devices.items())},
            "merchants": {k: v.to_canonical_dict() for k, v in sorted(self._merchants.items())},
            "gateways": {k: v.to_canonical_dict() for k, v in sorted(self._gateways.items())},
            "scheduler_queue_size": self._env.queue_size,
            "scheduler_step_count": self._env.step_count,
            "sim_time_ns": self._env.now,
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return raw.encode()

    def get_state_hash(self) -> str:
        state_bytes = self.get_canonical_state_bytes() + self._rng.get_state()
        return hashlib.sha256(state_bytes).hexdigest()

    def get_full_snapshot_bytes(self) -> bytes:
        """Full-fidelity serialization of aggregate state, restorable via
        restore_full_snapshot_bytes() — distinct from get_canonical_state_bytes(),
        which drops fields (account_id, owner_id, created_at_ns, ...) that
        aren't needed for hashing but are required to reconstruct a live
        Account/Device/Merchant/etc. Used by build_simulation_for_branch()
        (sim/main.py) to rebuild a running engine from a ChronoDAG checkpoint
        in a separate process — see docs/redteam_agent_design.md §3/§6 Phase 3.

        Pickle rather than a hand-written per-aggregate-type dict: these are
        plain in-memory Python objects with no external resources, and this
        matches the existing checkpointing convention in
        DeterministicRNG.get_state()/set_state() (sim/scheduler/rng.py).
        """
        state = {
            "accounts": self._accounts,
            "payments": self._payments,
            "devices": self._devices,
            "merchants": self._merchants,
            "gateways": self._gateways,
            "settlement_batches": self._settlement_batches,
            "seq_num": self._seq_num,
            "tx_counter": self._tx_counter,
            "processed_idempotency_keys": self._processed_idempotency_keys,
            "global_params": self._global_params,
        }
        return pickle.dumps(state)

    def restore_full_snapshot_bytes(self, data: bytes) -> None:
        """Restore aggregate state produced by get_full_snapshot_bytes().

        Replaces this engine's in-memory aggregates wholesale — intended to
        be called once, immediately after construction, before any commands
        are executed. Does not touch the scheduler queue, RNG state, or
        ChronoDAG wiring; callers restore those separately (see
        build_simulation_for_branch() in sim/main.py).
        """
        state = pickle.loads(data)  # noqa: S301
        self._accounts = state["accounts"]
        self._payments = state["payments"]
        self._devices = state["devices"]
        self._merchants = state["merchants"]
        self._gateways = state["gateways"]
        self._settlement_batches = state["settlement_batches"]
        self._seq_num = state["seq_num"]
        self._tx_counter = state["tx_counter"]
        self._processed_idempotency_keys = state["processed_idempotency_keys"]
        self._global_params = state["global_params"]

    def _next_event_id(self) -> str:
        import uuid
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{self._branch_id}:{self._seq_num}"))

    def _next_seq_num(self) -> int:
        self._seq_num += 1
        return self._seq_num

    def _next_tx_id(self) -> str:
        """Deterministic tx_id, derived like _next_event_id() instead of
        uuid.uuid4() (which is os.urandom-backed and breaks reproducibility —
        this was silently non-deterministic until real transactions started
        flowing through here)."""
        self._tx_counter += 1
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{self._branch_id}:tx:{self._tx_counter}"))

    def _create_event(self, event_class: type[Any], **kwargs: Any) -> Any:
        return event_class(
            event_id=self._next_event_id(),
            event_type=event_class.__name__,
            sim_time_ns=self.sim_time_ns,
            branch_id=self._branch_id,
            seq_num=self._next_seq_num(),
            **kwargs
        )

    def _get_daily_reset_events(self, account_ids: list[str], actor_id: str | None) -> list[DomainEvent]:
        events = []
        for acc_id in account_ids:
            if not acc_id:
                continue
            acc = self._accounts.get(acc_id)
            if acc:
                reset_event = acc._maybe_reset_daily_counters(self.sim_time_ns, dry_run=False)
                if reset_event:
                    # DomainEvent (and DailyCountersReset) is a frozen
                    # dataclass — _maybe_reset_daily_counters() returns one
                    # with placeholder event_id/branch_id/seq_num/actor_id
                    # (comment there: "Populated by Engine"), but direct
                    # attribute assignment on a frozen instance raises
                    # dataclasses.FrozenInstanceError. This was crashing
                    # the main population loop on every daily-counter
                    # rollover. dataclasses.replace() is the frozen-safe
                    # equivalent of the mutation this was trying to do.
                    reset_event = dataclasses.replace(
                        reset_event,
                        event_id=self._next_event_id(),
                        branch_id=self._branch_id,
                        seq_num=self._next_seq_num(),
                        actor_id=actor_id,
                    )
                    events.append(reset_event)
        return events

    def _apply_event(self, event: DomainEvent) -> None:
        start = time.perf_counter()
        try:
            self._apply_event_inner(event)
        finally:
            EVENT_LATENCY.labels(event_type=event.event_type).observe(time.perf_counter() - start)
            EVENTS_PROCESSED.labels(event_type=event.event_type, branch_id=event.branch_id).inc()

    def _apply_event_inner(self, event: DomainEvent) -> None:
        if isinstance(event, AccountCreated):
            self._accounts[event.account_id] = Account(event)
        elif isinstance(event, DeviceRegistered):
            self._devices[event.device_id] = Device(event)
        elif isinstance(event, MerchantOnboarded):
            self._merchants[event.merchant_id] = Merchant(event)
        elif isinstance(event, PaymentRequested):
            self._payments[event.tx_id] = Payment(event)
            # if auto capture, apply it here so we have the aggregate created
        elif isinstance(event, SettlementBatchCreated):
            self._settlement_batches[event.batch_id] = SettlementBatch(event)
        elif isinstance(event, GatewayStatusChanged):
            if event.gateway_id not in self._gateways:
                self._gateways[event.gateway_id] = GatewayEntity(event.gateway_id)
            self._gateways[event.gateway_id].apply_event(event)
        elif isinstance(event, DailyCountersReset) and event.account_id in self._accounts:
            self._accounts[event.account_id].apply_event(event)

        account_id = getattr(event, "account_id", None)
        if account_id and account_id in self._accounts:
            self._accounts[account_id].apply_event(event)

        tx_id = getattr(event, "tx_id", None)
        if tx_id and tx_id in self._payments:
            self._payments[tx_id].apply_event(event)

        # Handle specific events that affect multiple entities or require special logic
        if isinstance(event, (AccountCredited, AccountDebited, TransferRejected)):
            src_id = getattr(event, "source_account_id", None)
            if src_id and src_id in self._accounts:
                self._accounts[src_id].apply_event(event)

            tgt_id = getattr(event, "target_account_id", None)
            if tgt_id and tgt_id in self._accounts:
                self._accounts[tgt_id].apply_event(event)

    def _apply_events(self, events: list[DomainEvent]) -> None:
        for event in events:
            self._apply_event(event)

    _ENVELOPE_FIELDS = frozenset({
        "event_id", "event_type", "sim_time_ns", "actor_id",
        "branch_id", "seq_num", "causation_id", "correlation_id",
    })

    @staticmethod
    def _jsonify(value: Any) -> Any:
        if isinstance(value, enum.Enum):
            return value.value
        if isinstance(value, dict):
            return {k: WorldEngineImpl._jsonify(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [WorldEngineImpl._jsonify(v) for v in value]
        return value

    @classmethod
    def _event_payload(cls, event: DomainEvent) -> dict[str, object]:
        raw = dataclasses.asdict(event)
        return {k: cls._jsonify(v) for k, v in raw.items() if k not in cls._ENVELOPE_FIELDS}

    def _persist_events(self, events: list[DomainEvent]) -> None:
        """Append events to the ChronoDAG store, per the plan's Emit -> Append pipeline stage.

        No-op if no ChronoDAG was wired in (e.g. isolated unit tests).
        """
        if self._chrono is None or not events:
            return

        stored_events = [
            StoredEvent(
                event_id=event.event_id,
                event_type=event.event_type,
                sim_time_ns=event.sim_time_ns,
                actor_id=event.actor_id,
                branch_id=event.branch_id,
                seq_num=event.seq_num,
                payload=self._event_payload(event),
                causation_id=event.causation_id,
                correlation_id=event.correlation_id,
            )
            for event in events
        ]
        if hasattr(self._chrono, "save_events"):
            self._chrono.save_events(stored_events)
        else:
            for se in stored_events:
                self._chrono.save_event(se)

    def _mask_owner_id(self, owner_id: str) -> str:
        return hashlib.sha256(owner_id.encode()).hexdigest()[:8]

    # Handlers
    def _assess_risk(self, command: Command, tx_id: str, src: Account) -> RiskDecision:
        """Ask the injected risk scorer about one transaction.

        Returns ALLOW when no scorer is wired, so the engine emits exactly what
        it emitted before risk scoring existed and every replay and determinism
        test stays valid.

        A scorer that raises is treated as ALLOW rather than propagating. A risk
        model is advisory: if it breaks, payments must keep clearing. The
        alternative - an exception escaping into the command pipeline - turns a
        bad model into a total outage, which is a worse failure than missing
        some fraud.
        """
        if self._risk is None:
            return RiskDecision.allow(rail="none")

        device = self._devices.get(command.device_id or "")
        destination = self._accounts.get(command.target_account_id or "")
        context = RiskContext(
            tx_id=tx_id,
            tx_type=command.action_type,
            actor_id=command.actor_id,
            source_account_id=src.account_id,
            destination_account_id=command.target_account_id or "",
            amount_paise=command.amount_paise,
            sim_time_ns=self.sim_time_ns,
            gateway_id=command.gateway_hint,
            device_type=device.device_type if device else None,
            source_account_type=src.account_type,
            source_kyc_level=src.kyc_level,
            destination_account_type=destination.account_type if destination else None,
            destination_status=destination.status if destination else None,
            source_owner_id=src.owner_id,
            destination_owner_id=destination.owner_id if destination else "",
        )
        try:
            return self._risk.assess(context)
        except Exception:
            return RiskDecision.allow(rail="none", reason="risk scorer raised")

    def _execute_transfer(self, command: Command) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        if not command.source_account_id or not command.target_account_id:
            return events

        src = self._accounts.get(command.source_account_id)
        dst = self._accounts.get(command.target_account_id)

        if not src or not dst:
            return events

        # A real payments system never lets you name an arbitrary account
        # as the source of a transfer — only its owner can spend from it.
        # This was missing entirely until a red-team session found it
        # (naming another account as source_account_id silently drained
        # it, no different from a normal transfer) — see
        # docs/redteam_agent_design.md and WorldEngine.get_world_view()'s
        # docstring, both updated once this was closed. Visibility into
        # other accounts (who you can PAY) is unaffected; this only gates
        # who can be the SOURCE.
        if command.actor_id != src.owner_id:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id=dst.account_id,
                amount_paise=command.amount_paise, reason_code="UNAUTHORIZED_SOURCE",
                detail=f"actor {command.actor_id!r} does not own source account {src.account_id!r}"
            ))
            return events

        # Validate
        reason = src.can_debit(command.amount_paise)
        if reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id=dst.account_id,
                amount_paise=command.amount_paise, reason_code="DEBIT_REJECTED", detail=reason
            ))
            return events

        limit_reason = src.check_daily_limit(command.amount_paise, self.sim_time_ns)
        if limit_reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id=dst.account_id,
                amount_paise=command.amount_paise, reason_code="LIMIT_EXCEEDED", detail=limit_reason
            ))
            return events

        inbound_limit_reason = dst.check_inbound_daily_limit(command.amount_paise, self.sim_time_ns)
        if inbound_limit_reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id=dst.account_id,
                amount_paise=command.amount_paise, reason_code="LIMIT_EXCEEDED", detail=inbound_limit_reason
            ))
            return events

        tx_id = self._next_tx_id()

        # The wire rail sees every transfer and stops none of them. Money
        # laundering detection runs at roughly 12% precision, so an automatic
        # block would refuse about eight innocent transfers for every real
        # laundering leg. A flagged transfer is queued for a human instead, and
        # the freeze that actually holds funds is a separate action a named
        # reviewer takes. The result is deliberately not read here: the scorer
        # needs the transfer to build its account graph, and that is the whole
        # purpose of the call.
        self._assess_risk(command, tx_id, src)

        # Debits and credits
        events.append(self._create_event(
            AccountDebited, actor_id=command.actor_id,
            account_id=src.account_id, amount_paise=command.amount_paise,
            tx_id=tx_id, reason="Transfer out"
        ))

        events.append(self._create_event(
            AccountCredited, actor_id=command.actor_id,
            account_id=dst.account_id, amount_paise=command.amount_paise,
            tx_id=tx_id, reason="Transfer in"
        ))

        return events

    def _execute_payment(self, command: Command) -> list[DomainEvent]:
        events: list[DomainEvent] = []

        tx_id = self._next_tx_id()
        events.append(self._create_event(
            PaymentRequested, actor_id=command.actor_id, tx_id=tx_id,
            tx_type=TransactionType.PAYMENT, source_account_id=command.source_account_id or "",
            destination_account_id=command.target_account_id or "", amount_paise=command.amount_paise,
            gateway_id=command.gateway_hint, idempotency_key=command.idempotency_key
        ))

        # In a real system, we would wait for auth. For this basic handler, we simulate auth failure if balance low
        src = self._accounts.get(command.source_account_id or "")
        if not src:
            events.append(self._create_event(
                PaymentDeclined, actor_id=command.actor_id, tx_id=tx_id,
                reason="Invalid source account", decline_code="INVALID_ACCOUNT"
            ))
        elif command.actor_id != src.owner_id:
            # Same ownership rule as _execute_transfer() above — only the
            # source account's owner can pay out of it.
            events.append(self._create_event(
                PaymentDeclined, actor_id=command.actor_id, tx_id=tx_id,
                reason=f"actor {command.actor_id!r} does not own source account {src.account_id!r}",
                decline_code="UNAUTHORIZED_SOURCE"
            ))
        elif src.can_debit(command.amount_paise):
            events.append(self._create_event(
                PaymentDeclined, actor_id=command.actor_id, tx_id=tx_id,
                reason="Insufficient funds", decline_code="INSUFFICIENT_FUNDS"
            ))
        elif (dst := self._accounts.get(command.target_account_id or "")) and (inbound_limit_reason := dst.check_inbound_daily_limit(command.amount_paise, self.sim_time_ns)):
            events.append(self._create_event(
                PaymentDeclined, actor_id=command.actor_id, tx_id=tx_id,
                reason=inbound_limit_reason, decline_code="LIMIT_EXCEEDED"
            ))
        elif (risk := self._assess_risk(command, tx_id, src)).action is RiskAction.BLOCK:
            # Risk is consulted last, after ownership and funds. A payment that
            # was going to fail anyway must not be scored: it would teach the
            # model that "declined for no money" looks like fraud, and it would
            # let an attacker probe the risk system for free with payments they
            # cannot fund.
            events.append(self._create_event(
                PaymentDeclined, actor_id=command.actor_id, tx_id=tx_id,
                reason=risk.reason or "Blocked by risk assessment",
                decline_code="RISK_BLOCKED"
            ))
        else:
            events.append(self._create_event(
                PaymentAuthorized, actor_id=command.actor_id, tx_id=tx_id, gateway_id=command.gateway_hint or ""
            ))
            # Schedule a capture after some time, or capture immediately based on the payment properties
            # omitting scheduling for brevity, assuming capture is immediate in this basic test implementation

        return events

    def _execute_cash_in(self, command: Command) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        if not command.target_account_id:
            return events

        dst = self._accounts.get(command.target_account_id)
        if not dst:
            return events

        inbound_limit_reason = dst.check_inbound_daily_limit(command.amount_paise, self.sim_time_ns)
        if inbound_limit_reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id="CASH_ENTITY", target_account_id=dst.account_id,
                amount_paise=command.amount_paise, reason_code="LIMIT_EXCEEDED", detail=inbound_limit_reason
            ))
            return events

        tx_id = self._next_tx_id()
        events.append(self._create_event(
            AccountCredited, actor_id=command.actor_id,
            account_id=dst.account_id, amount_paise=command.amount_paise,
            tx_id=tx_id, reason="Cash in"
        ))
        return events

    def _execute_cash_out(self, command: Command) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        if not command.source_account_id:
            return events

        src = self._accounts.get(command.source_account_id)
        if not src:
            return events

        reason = src.can_debit(command.amount_paise)
        if reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id="CASH_ENTITY",
                amount_paise=command.amount_paise, reason_code="DEBIT_REJECTED", detail=reason
            ))
            return events

        limit_reason = src.check_daily_limit(command.amount_paise, self.sim_time_ns)
        if limit_reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id="CASH_ENTITY",
                amount_paise=command.amount_paise, reason_code="LIMIT_EXCEEDED", detail=limit_reason
            ))
            return events

        tx_id = self._next_tx_id()

        # Cash-out is the exit. Laundering ends here - value pools in a mule
        # account and leaves the system - and the rails were watching the money
        # arrive and then losing sight of it at the one moment that completes
        # the pattern. Advisory like the wire rail: the result is not read,
        # because stopping a withdrawal on a model's say-so has the same
        # precision problem and the same tipping-off exposure as stopping a
        # transfer.
        self._assess_risk(command, tx_id, src)

        events.append(self._create_event(
            AccountDebited, actor_id=command.actor_id,
            account_id=src.account_id, amount_paise=command.amount_paise,
            tx_id=tx_id, reason="Cash out"
        ))
        return events

    def _execute_debit(self, command: Command) -> list[DomainEvent]:
        # DEBIT: External -> Account (inflow, e.g. salary, ACH pull)
        events: list[DomainEvent] = []
        if not command.target_account_id:
            return events

        dst = self._accounts.get(command.target_account_id)
        if not dst:
            return events

        inbound_limit_reason = dst.check_inbound_daily_limit(command.amount_paise, self.sim_time_ns)
        if inbound_limit_reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id="EXTERNAL", target_account_id=dst.account_id,
                amount_paise=command.amount_paise, reason_code="LIMIT_EXCEEDED", detail=inbound_limit_reason
            ))
            return events

        tx_id = self._next_tx_id()
        events.append(self._create_event(
            AccountCredited, actor_id=command.actor_id,
            account_id=dst.account_id, amount_paise=command.amount_paise,
            tx_id=tx_id, reason="External debit/inflow"
        ))
        return events

    def _execute_refund(self, command: Command) -> list[DomainEvent]:
        # REFUND: Merchant (source) -> User (target)
        return self._execute_transfer(command)

    def _execute_chargeback(self, command: Command) -> list[DomainEvent]:
        # CHARGEBACK: forced reversal, pulls funds from source (merchant) back to target (payer)
        events: list[DomainEvent] = []
        if not command.source_account_id or not command.target_account_id:
            return events

        src = self._accounts.get(command.source_account_id)
        dst = self._accounts.get(command.target_account_id)
        if not src or not dst:
            return events

        reason = src.can_debit(command.amount_paise)
        if reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id=dst.account_id,
                amount_paise=command.amount_paise, reason_code="DEBIT_REJECTED", detail=reason
            ))
            return events

        tx_id = str(command.metadata.get("tx_id", self._next_tx_id()))
        events.append(self._create_event(
            AccountDebited, actor_id=command.actor_id,
            account_id=src.account_id, amount_paise=command.amount_paise,
            tx_id=tx_id, reason="Chargeback debit"
        ))
        events.append(self._create_event(
            AccountCredited, actor_id=command.actor_id,
            account_id=dst.account_id, amount_paise=command.amount_paise,
            tx_id=tx_id, reason="Chargeback credit"
        ))
        events.append(self._create_event(
            PaymentChargedBack, actor_id=command.actor_id, tx_id=tx_id,
            chargeback_id=self._next_tx_id(),
            reason=str(command.metadata.get("reason", "forced_reversal")),
            amount_paise=command.amount_paise
        ))
        return events

    def _execute_settlement(self, command: Command) -> list[DomainEvent]:
        # SETTLEMENT: internal rail batch clearing, credits the target (merchant) account,
        # optionally debiting a source (e.g. gateway settlement) account first.
        events: list[DomainEvent] = []
        if not command.target_account_id:
            return events

        dst = self._accounts.get(command.target_account_id)
        if not dst:
            return events

        batch_id = str(command.metadata.get("batch_id", self._next_tx_id()))
        fee_paise = int(str(command.metadata.get("fee_paise", 0)))
        net_amount_paise = command.amount_paise - fee_paise

        if command.source_account_id:
            src = self._accounts.get(command.source_account_id)
            if src:
                reason = src.can_debit(command.amount_paise)
                if reason:
                    events.append(self._create_event(
                        TransferRejected, actor_id=command.actor_id,
                        source_account_id=src.account_id, target_account_id=dst.account_id,
                        amount_paise=command.amount_paise, reason_code="DEBIT_REJECTED", detail=reason
                    ))
                    return events
                events.append(self._create_event(
                    AccountDebited, actor_id=command.actor_id,
                    account_id=src.account_id, amount_paise=command.amount_paise,
                    tx_id=batch_id, reason="Settlement debit"
                ))

        events.append(self._create_event(
            AccountCredited, actor_id=command.actor_id,
            account_id=dst.account_id, amount_paise=net_amount_paise,
            tx_id=batch_id, reason="Settlement credit"
        ))
        events.append(self._create_event(
            SettlementBatchCreated, actor_id=command.actor_id, batch_id=batch_id,
            gateway_id=command.gateway_hint or "", merchant_id=dst.account_id,
            tx_count=int(str(command.metadata.get("tx_count", 1))),
            total_amount_paise=command.amount_paise, fee_total_paise=fee_paise,
            net_amount_paise=net_amount_paise
        ))
        events.append(self._create_event(
            SettlementBatchCompleted, actor_id=command.actor_id, batch_id=batch_id
        ))
        return events

    def _execute_fee(self, command: Command) -> list[DomainEvent]:
        # FEE: rail/gateway charge, debits the account the fee applies to
        events: list[DomainEvent] = []
        if not command.source_account_id:
            return events

        src = self._accounts.get(command.source_account_id)
        if not src:
            return events

        reason = src.can_debit(command.amount_paise)
        if reason:
            events.append(self._create_event(
                TransferRejected, actor_id=command.actor_id,
                source_account_id=src.account_id, target_account_id="FEE_ENTITY",
                amount_paise=command.amount_paise, reason_code="DEBIT_REJECTED", detail=reason
            ))
            return events

        fee_tx_id = self._next_tx_id()
        events.append(self._create_event(
            AccountDebited, actor_id=command.actor_id,
            account_id=src.account_id, amount_paise=command.amount_paise,
            tx_id=fee_tx_id, reason="Fee charge"
        ))
        events.append(self._create_event(
            FeeCharged, actor_id=command.actor_id, fee_tx_id=fee_tx_id,
            source_tx_id=str(command.metadata.get("source_tx_id", "")),
            account_id=src.account_id, amount_paise=command.amount_paise,
            fee_type=str(command.metadata.get("fee_type", "gateway"))
        ))
        return events

    def _execute_interest(self, command: Command) -> list[DomainEvent]:
        # INTEREST: savings/loan accrual, credits the account
        events: list[DomainEvent] = []
        if not command.target_account_id:
            return events

        dst = self._accounts.get(command.target_account_id)
        if not dst:
            return events

        events.append(self._create_event(
            AccountCredited, actor_id=command.actor_id,
            account_id=dst.account_id, amount_paise=command.amount_paise,
            tx_id=self._next_tx_id(), reason="Interest accrual"
        ))
        events.append(self._create_event(
            InterestAccrued, actor_id=command.actor_id, account_id=dst.account_id,
            amount_paise=command.amount_paise,
            rate_bps=int(str(command.metadata.get("rate_bps", 0))),
            period_ns=float(str(command.metadata.get("period_ns", 0.0)))
        ))
        return events
