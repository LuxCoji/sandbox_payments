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
    DeviceRegistered,
    DomainEvent,
    FeeCharged,
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
from sim.core.interfaces import (
    AccountSnapshot,
    AccountType,
    ActorRole,
    Command,
    CommandResult,
    GlobalParams,
    TransactionType,
    WorldView,
)
from sim.core.merchant import Merchant
from sim.core.payment import Payment
from sim.observability import EVENT_LATENCY, EVENTS_PROCESSED, SCHEDULER_QUEUE_SIZE, traced

if TYPE_CHECKING:
    from sim.core.gateway import GatewayEntity
    from sim.core.settlement import SettlementBatch
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
    ) -> None:
        self._env = env
        self._rng = rng
        self._branch_id = branch_id
        self._chrono = chrono
        self._seq_num: int = seq_num

        self._accounts: dict[str, Account] = {}
        self._payments: dict[str, Payment] = {}
        self._devices: dict[str, Device] = {}
        self._merchants: dict[str, Merchant] = {}
        self._gateways: dict[str, GatewayEntity] = {}
        self._settlement_batches: dict[str, SettlementBatch] = {}

        self._global_params = global_params or GlobalParams(
            fee_schedules=(), rail_limits=(), settlement_cut_off_ns=0.0
        )

        self._processed_idempotency_keys: dict[str, None] = {}
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
        if actor_role in (ActorRole.USER, ActorRole.MERCHANT, ActorRole.RED_AGENT):
            for acc in self._accounts.values():
                if acc.owner_id == actor_id:
                    visible_accounts.append(acc.to_snapshot())
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
            return CommandResult(events=(), success=True)

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

        handler = getattr(self, handler_name)
        events = handler(command)

        self._persist_events(events)
        self._apply_events(events)
        self._processed_idempotency_keys[command.idempotency_key] = None
        if len(self._processed_idempotency_keys) > 10000:
            self._processed_idempotency_keys.pop(next(iter(self._processed_idempotency_keys)))

        rejection_types = {
            "PaymentDeclined", "TransferRejected", "RefundRejected",
            "AccountFreezeFailed", "PaymentTimeout",
        }
        success = len(events) > 0 and all(type(e).__name__ not in rejection_types for e in events)

        return CommandResult(events=tuple(events), success=success)

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

    def get_state_hash(self) -> str:
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
        state_bytes = raw.encode() + self._rng.get_state()
        return hashlib.sha256(state_bytes).hexdigest()

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
        if self._chrono is None:
            return
        for event in events:
            self._chrono.save_event(StoredEvent(
                event_id=event.event_id,
                event_type=event.event_type,
                sim_time_ns=event.sim_time_ns,
                actor_id=event.actor_id,
                branch_id=event.branch_id,
                seq_num=event.seq_num,
                payload=self._event_payload(event),
                causation_id=event.causation_id,
                correlation_id=event.correlation_id,
            ))

    def _mask_owner_id(self, owner_id: str) -> str:
        return hashlib.sha256(owner_id.encode()).hexdigest()[:8]

    # Handlers
    def _execute_transfer(self, command: Command) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        if not command.source_account_id or not command.target_account_id:
            return events

        src = self._accounts.get(command.source_account_id)
        dst = self._accounts.get(command.target_account_id)

        if not src or not dst:
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

        tx_id = self._next_tx_id()

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
        elif src.can_debit(command.amount_paise):
            events.append(self._create_event(
                PaymentDeclined, actor_id=command.actor_id, tx_id=tx_id,
                reason="Insufficient funds", decline_code="INSUFFICIENT_FUNDS"
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
