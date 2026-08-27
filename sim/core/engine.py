"""WorldEngine implementation — the core simulation engine.

Owns the SimulationEnv and all in-memory aggregate projections.
Mutates state strictly via domain events through apply_event pipeline.
Has ZERO dependency on ChronoDAG.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING

from sim.core.account import Account
from sim.core.device import Device
from sim.core.events import (
    AccountCreated,
    AccountCredited,
    AccountDebited,
    DeviceRegistered,
    DomainEvent,
    MerchantOnboarded,
    PaymentAuthorized,
    PaymentDeclined,
    PaymentRequested,
    TransferRejected,
)
from sim.core.interfaces import (
    AccountSnapshot,
    ActorRole,
    Command,
    CommandResult,
    GlobalParams,
    TransactionType,
    WorldView,
)
from sim.core.merchant import Merchant
from sim.core.payment import Payment

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
    ) -> None:
        self._env = env
        self._rng = rng
        self._branch_id = branch_id
        self._seq_num: int = 0

        self._accounts: dict[str, Account] = {}
        self._payments: dict[str, Payment] = {}
        self._devices: dict[str, Device] = {}
        self._merchants: dict[str, Merchant] = {}
        self._gateways: dict[str, GatewayEntity] = {}
        self._settlement_batches: dict[str, SettlementBatch] = {}

        self._global_params = global_params or GlobalParams(
            fee_schedules=(), rail_limits=(), settlement_cut_off_ns=0.0
        )

        self._processed_idempotency_keys: set[str] = set()

    @property
    def sim_time_ns(self) -> float:
        return self._env.now

    def schedule_event(self, event: ScheduledEvent) -> None:
        self._env.schedule(event)

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
            # PII masking
            for i, acc in enumerate(self._accounts.values()):
                if i < offset:
                    continue
                if len(visible_accounts) >= limit:
                    break
                snap = acc.to_snapshot()
                # Use replace instead of mutation as snapshot is frozen (dataclasses.replace)
                from dataclasses import replace
                masked_snap = replace(
                    snap,
                    account_id=snap.account_id,  # Leave ID as is, mask owner_id downstream if part of it
                    # But AccountSnapshot doesn't have owner_id directly! It only has account_id and linked_device_ids.
                    # linked_device_ids are hashed
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
        }.get(command.action_type)

        if not handler_name:
            raise ValueError(f"Unsupported action type: {command.action_type}")

        handler = getattr(self, handler_name)
        events = handler(command)

        self._apply_events(events)
        self._processed_idempotency_keys.add(command.idempotency_key)

        rejection_types = {
            "PaymentDeclined", "TransferRejected", "RefundRejected",
            "AccountFreezeFailed", "PaymentTimeout",
        }
        success = any(type(e).__name__ not in rejection_types for e in events)

        return CommandResult(events=tuple(events), success=success)

    def get_state_hash(self) -> str:
        canonical = {
            "accounts": {k: v.to_canonical_dict() for k, v in sorted(self._accounts.items())},
            "payments": {k: v.to_canonical_dict() for k, v in sorted(self._payments.items())},
            "devices": {k: v.to_canonical_dict() for k, v in sorted(self._devices.items())},
            "merchants": {k: v.to_canonical_dict() for k, v in sorted(self._merchants.items())},
            "gateways": {k: v.to_canonical_dict() for k, v in sorted(self._gateways.items())},
            "settlement_batches": {
                k: v.to_canonical_dict() for k, v in sorted(self._settlement_batches.items())
            },
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

    def _create_event(self, event_class, **kwargs):
        return event_class(
            event_id=self._next_event_id(),
            event_type=event_class.__name__,
            sim_time_ns=self.sim_time_ns,
            branch_id=self._branch_id,
            seq_num=self._next_seq_num(),
            **kwargs
        )

    def _apply_event(self, event: DomainEvent) -> None:
        if isinstance(event, AccountCreated):
            self._accounts[event.account_id] = Account(event)
        elif isinstance(event, DeviceRegistered):
            self._devices[event.device_id] = Device(event)
        elif isinstance(event, MerchantOnboarded):
            self._merchants[event.merchant_id] = Merchant(event)
        elif isinstance(event, PaymentRequested):
            self._payments[event.tx_id] = Payment(event)
            # if auto capture, apply it here so we have the aggregate created

        # Route to appropriate aggregate
        if hasattr(event, "account_id") and event.account_id in self._accounts:
            self._accounts[event.account_id].apply_event(event)
        if hasattr(event, "tx_id") and event.tx_id in self._payments:
            self._payments[event.tx_id].apply_event(event)

        # Handle specific events that affect multiple entities or require special logic
        if isinstance(event, (AccountCredited, AccountDebited, TransferRejected)):
            if hasattr(event, "source_account_id") and event.source_account_id in self._accounts:
                self._accounts[event.source_account_id].apply_event(event)
            if hasattr(event, "target_account_id") and event.target_account_id in self._accounts:
                self._accounts[event.target_account_id].apply_event(event)

    def _apply_events(self, events: list[DomainEvent]) -> None:
        for event in events:
            self._apply_event(event)

    def _mask_owner_id(self, owner_id: str) -> str:
        return hashlib.sha256(owner_id.encode()).hexdigest()[:8]

    # Handlers
    def _execute_transfer(self, command: Command) -> list[DomainEvent]:
        events = []
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

        tx_id = str(uuid.uuid4())

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
        events = []
        tx_id = str(uuid.uuid4())
        events.append(self._create_event(
            PaymentRequested, actor_id=command.actor_id, tx_id=tx_id,
            tx_type=TransactionType.PAYMENT, source_account_id=command.source_account_id or "",
            destination_account_id=command.target_account_id or "", amount_paise=command.amount_paise,
            gateway_id=command.gateway_hint, idempotency_key=command.idempotency_key
        ))

        # In a real system, we would wait for auth. For this basic handler, we simulate auth failure if balance low
        src = self._accounts.get(command.source_account_id or "")
        if src and src.can_debit(command.amount_paise):
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
        return []

    def _execute_cash_out(self, command: Command) -> list[DomainEvent]:
        return []

    def _execute_debit(self, command: Command) -> list[DomainEvent]:
        return []

    def _execute_refund(self, command: Command) -> list[DomainEvent]:
        return []
