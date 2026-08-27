"""Domain event definitions.

Events are the single source of truth. In-memory aggregate projections
are updated strictly by applying emitted events via event handlers.

Envelope fields (common to all events):
    event_id:       UUIDv7 unique identifier
    event_type:     Discriminator string (e.g., "AccountCreated")
    sim_time_ns:    Simulation timestamp in nanoseconds
    actor_id:       ID of the actor who caused this event (None for system events)
    branch_id:      ChronoDAG branch this event belongs to
    seq_num:        Monotonic sequence number within the branch
    causation_id:   Event ID of the triggering event (causal chain)
    correlation_id: Shared ID grouping a multi-step flow
"""
from __future__ import annotations

from dataclasses import dataclass

from sim.core.interfaces import (
    AccountStatus,
    AccountType,
    DeviceStatus,
    DeviceType,
    TransactionType,
)

# ── Base Envelope ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DomainEvent:
    """Base envelope for all domain events."""
    event_id: str
    event_type: str
    sim_time_ns: float
    actor_id: str | None
    branch_id: str
    seq_num: int
    causation_id: str | None = None
    correlation_id: str | None = None


# ── Account Events ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccountCreated(DomainEvent):
    account_id: str = ""
    account_type: AccountType = AccountType.PERSONAL
    initial_balance_paise: int = 0
    kyc_level: int = 0
    owner_id: str = ""


@dataclass(frozen=True)
class AccountCredited(DomainEvent):
    account_id: str = ""
    amount_paise: int = 0
    tx_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AccountDebited(DomainEvent):
    account_id: str = ""
    amount_paise: int = 0
    tx_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AccountFrozen(DomainEvent):
    account_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AccountClosed(DomainEvent):
    account_id: str = ""


@dataclass(frozen=True)
class AccountStatusChanged(DomainEvent):
    account_id: str = ""
    old_status: AccountStatus = AccountStatus.ACTIVE
    new_status: AccountStatus = AccountStatus.ACTIVE
    reason: str = ""


@dataclass(frozen=True)
class KycLevelChanged(DomainEvent):
    account_id: str = ""
    old_level: int = 0
    new_level: int = 0


# ── Payment Events ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PaymentRequested(DomainEvent):
    tx_id: str = ""
    tx_type: TransactionType = TransactionType.PAYMENT
    source_account_id: str = ""
    destination_account_id: str = ""
    amount_paise: int = 0
    gateway_id: str | None = None
    idempotency_key: str = ""


@dataclass(frozen=True)
class PaymentAuthorized(DomainEvent):
    tx_id: str = ""
    gateway_id: str = ""


@dataclass(frozen=True)
class PaymentDeclined(DomainEvent):
    tx_id: str = ""
    reason: str = ""
    decline_code: str = ""


@dataclass(frozen=True)
class PaymentCaptured(DomainEvent):
    tx_id: str = ""


@dataclass(frozen=True)
class PaymentSettled(DomainEvent):
    tx_id: str = ""
    fee_paise: int = 0
    net_amount_paise: int = 0


@dataclass(frozen=True)
class PaymentCompleted(DomainEvent):
    tx_id: str = ""


@dataclass(frozen=True)
class PaymentRefunded(DomainEvent):
    tx_id: str = ""
    refund_tx_id: str = ""
    refund_amount_paise: int = 0
    is_partial: bool = False


@dataclass(frozen=True)
class PaymentVoided(DomainEvent):
    tx_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PaymentChargedBack(DomainEvent):
    tx_id: str = ""
    chargeback_id: str = ""
    reason: str = ""
    amount_paise: int = 0


# ── Device Events ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DeviceRegistered(DomainEvent):
    device_id: str = ""
    owner_id: str = ""
    device_type: DeviceType = DeviceType.MOBILE


@dataclass(frozen=True)
class DeviceStatusChanged(DomainEvent):
    device_id: str = ""
    old_status: DeviceStatus = DeviceStatus.ACTIVE
    new_status: DeviceStatus = DeviceStatus.ACTIVE
    reason: str = ""


# ── Merchant Events ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class MerchantOnboarded(DomainEvent):
    merchant_id: str = ""
    name: str = ""
    category: str = ""
    settlement_rail: str = ""


@dataclass(frozen=True)
class MerchantSuspended(DomainEvent):
    merchant_id: str = ""
    reason: str = ""


# ── Gateway Events ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GatewayStatusChanged(DomainEvent):
    gateway_id: str = ""
    old_status: str = ""
    new_status: str = ""
    reason: str = ""


# ── Settlement Events ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class SettlementBatchCreated(DomainEvent):
    batch_id: str = ""
    gateway_id: str = ""
    merchant_id: str = ""
    tx_count: int = 0
    total_amount_paise: int = 0
    fee_total_paise: int = 0
    net_amount_paise: int = 0


@dataclass(frozen=True)
class SettlementBatchCompleted(DomainEvent):
    batch_id: str = ""


# ── Fee Events ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeeCharged(DomainEvent):
    fee_tx_id: str = ""
    source_tx_id: str = ""
    account_id: str = ""
    amount_paise: int = 0
    fee_type: str = ""  # "gateway", "platform", "interchange"


# ── Interest Events ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class InterestAccrued(DomainEvent):
    account_id: str = ""
    amount_paise: int = 0
    rate_bps: int = 0
    period_ns: float = 0.0


# ── Counter Events ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyCountersReset(DomainEvent):
    account_id: str = ""
    old_daily_tx_count: int = 0
    old_daily_tx_volume_paise: int = 0


# ── Rejection Events ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PaymentTimeout(DomainEvent):
    tx_id: str = ""
    timeout_ns: float = 0.0


@dataclass(frozen=True)
class RefundRejected(DomainEvent):
    tx_id: str = ""
    requested_amount_paise: int = 0
    total_already_refunded_paise: int = 0
    reason: str = ""


@dataclass(frozen=True)
class TransferRejected(DomainEvent):
    source_account_id: str = ""
    target_account_id: str = ""
    amount_paise: int = 0
    reason_code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class AccountFreezeFailed(DomainEvent):
    account_id: str = ""
    current_status: AccountStatus = AccountStatus.ACTIVE
    reason: str = ""
