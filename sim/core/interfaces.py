"""Core subsystem contracts.

This module is the SINGLE SOURCE OF TRUTH for shared domain types.
All cross-subsystem imports target this module.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sim.core.events import DomainEvent
    from sim.scheduler.env import ScheduledEvent


# ── Enums ─────────────────────────────────────────────────────────────────

class AccountType(enum.Enum):
    PERSONAL = "PERSONAL"
    MERCHANT = "MERCHANT"
    CASH_ENTITY = "CASH_ENTITY"
    INTERNAL_SETTLEMENT = "INTERNAL_SETTLEMENT"
    ESCROW = "ESCROW"


class AccountStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"
    PENDING_KYC = "PENDING_KYC"
    DISPUTED = "DISPUTED"


class TransactionType(enum.Enum):
    """All action/transaction types in the system.

    Flows:
        PAYMENT:     User → Merchant (via gateway)
        TRANSFER:    Account → Account (peer-to-peer)
        CASH_IN:     Cash Entity → Account (deposit)
        CASH_OUT:    Account → Cash Entity (withdrawal)
        DEBIT:       External → Account (salary, income)
        REFUND:      Merchant → User
        CHARGEBACK:  Forced reversal (system-initiated)
        SETTLEMENT:  Internal rail (batch clearing)
        FEE:         Rail/gateway charge
        INTEREST:    Savings/loan accrual
    """
    PAYMENT = "PAYMENT"
    TRANSFER = "TRANSFER"
    CASH_IN = "CASH_IN"
    CASH_OUT = "CASH_OUT"
    DEBIT = "DEBIT"
    REFUND = "REFUND"
    CHARGEBACK = "CHARGEBACK"
    SETTLEMENT = "SETTLEMENT"
    FEE = "FEE"
    INTEREST = "INTEREST"


class PaymentStatus(enum.Enum):
    """Payment lifecycle states.

    State machine:
        INITIATED → AUTHORIZED → CAPTURED → SETTLED → COMPLETED
                  ↘ DECLINED
        AUTHORIZED → VOIDED
        CAPTURED → REFUNDED (partial or full)
        COMPLETED → CHARGED_BACK
    """
    INITIATED = "INITIATED"
    AUTHORIZED = "AUTHORIZED"
    DECLINED = "DECLINED"
    CAPTURED = "CAPTURED"
    VOIDED = "VOIDED"
    SETTLED = "SETTLED"
    COMPLETED = "COMPLETED"
    REFUNDED = "REFUNDED"
    CHARGED_BACK = "CHARGED_BACK"


class ActorRole(enum.Enum):
    """Roles determine visibility and capability grants.

    Visibility rules:
        USER:          Own accounts + public merchant directory
        MERCHANT:      Own accounts + payer pseudonymous IDs
        BANK_OPS:      All accounts (PII masked) + risk scores
        RISK_ANALYST:  Aggregated graph + model features
        RED_AGENT:     Tool responses + public data only
        BLUE_AGENT:    Alerts + case data
    """
    USER = "USER"
    MERCHANT = "MERCHANT"
    BANK_OPS = "BANK_OPS"
    RISK_ANALYST = "RISK_ANALYST"
    RED_AGENT = "RED_AGENT"
    BLUE_AGENT = "BLUE_AGENT"


class DeviceType(enum.Enum):
    MOBILE = "MOBILE"
    POS = "POS"
    ATM = "ATM"
    BROWSER = "BROWSER"


class DeviceStatus(enum.Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    LOST = "LOST"


# ── Immutable View Dataclasses ────────────────────────────────────────────

@dataclass(frozen=True)
class AccountSnapshot:
    """Immutable view of an account.

    `owner_id` is the real identity when this is the viewing actor's own
    account (true for every account a USER/MERCHANT sees, and for a
    RED_AGENT's own accounts within its otherwise cross-visible view). For
    every account that ISN'T the viewer's own — the rest of a RED_AGENT's
    view, and the entire view for BANK_OPS/RISK_ANALYST/BLUE_AGENT —
    WorldEngine.get_world_view() replaces it with a stable pseudonymous
    hash so no other actor's raw ID leaks through the snapshot.
    """
    account_id: str                          # UUIDv7
    account_type: AccountType
    balance_paise: int                       # INR paise (₹500.50 = 50050)
    status: AccountStatus
    kyc_level: int                           # 0 (none) to 3 (full)
    created_at: float                        # sim time nanoseconds
    daily_tx_count: int
    daily_tx_volume_paise: int
    linked_device_ids: tuple[str, ...]
    owner_id: str = ""                       # real ID (own view) or pseudonymous hash (masked view)
    merchant_category_code: str | None = None  # MCC, only for MERCHANT accounts


@dataclass(frozen=True)
class MerchantDirectoryEntry:
    """Public merchant info visible to all actors via the directory."""
    merchant_id: str                         # UUIDv7
    name: str
    category: str                            # MCC code or category string
    avg_rating: float
    settlement_rail: str


@dataclass(frozen=True)
class DeviceSnapshot:
    """Immutable view of a registered device."""
    device_id: str                           # UUIDv7
    owner_id: str
    device_type: DeviceType
    status: DeviceStatus
    registered_at: float                     # sim time nanoseconds


@dataclass(frozen=True)
class FeeSchedule:
    """Fee configuration for a transaction type through a rail/gateway."""
    tx_type: TransactionType
    gateway_id: str
    flat_fee_paise: int
    percentage_bps: int                      # basis points (100 bps = 1%)
    min_fee_paise: int
    max_fee_paise: int


@dataclass(frozen=True)
class RailLimits:
    """Transaction limits for a payment rail."""
    rail_id: str
    min_amount_paise: int
    max_amount_paise: int
    daily_limit_paise: int
    cut_off_time_ns: float                   # sim time nanoseconds


@dataclass(frozen=True)
class GlobalParams:
    """System-wide parameters visible to all actors."""
    fee_schedules: tuple[FeeSchedule, ...]
    rail_limits: tuple[RailLimits, ...]
    settlement_cut_off_ns: float


@dataclass(frozen=True)
class WorldView:
    """Immutable snapshot of the world as seen by a specific actor.

    Contains ONLY data the actor is authorized to see. For USER/MERCHANT
    that's their own accounts only. RED_AGENT and the cross-visibility ops
    roles (BANK_OPS/RISK_ANALYST/BLUE_AGENT) see every account on the
    branch, with other actors' owner_id/device ids replaced by a
    pseudonymous hash — RED_AGENT deliberately white-box (see
    WorldEngine.get_world_view), the ops roles because investigation needs
    full visibility. Also always included regardless of role:
    - Public merchant directory (name, category, rating, rail)
    - The caller's own registered devices
    - Global parameters (fee schedules, rail limits, cut-off times)
    """
    actor_id: str
    actor_role: ActorRole
    sim_time_ns: float
    accounts: tuple[AccountSnapshot, ...]
    merchants: tuple[MerchantDirectoryEntry, ...]
    devices: tuple[DeviceSnapshot, ...]
    global_params: GlobalParams


@dataclass(frozen=True)
class Command:
    """A validated action request to be processed by the engine.

    Constructed from a population Intent after validation.
    """
    command_id: str                          # UUIDv7
    actor_id: str
    action_type: TransactionType
    source_account_id: str | None
    target_account_id: str | None
    amount_paise: int
    idempotency_key: str
    device_id: str | None = None
    gateway_hint: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    """Result of executing a command against the engine."""
    events: tuple[DomainEvent, ...]
    success: bool


# ── Protocol ──────────────────────────────────────────────────────────────

@runtime_checkable
class WorldEngine(Protocol):
    """Protocol for the core simulation engine.

    The WorldEngine owns the single SimulationEnv instance and manages
    event execution. It holds in-memory aggregate state projections
    (Accounts, Devices, Merchants, Gateways) and mutates them strictly
    via domain events through the apply_event pipeline.
    """

    def get_world_view(
        self, actor_id: str, actor_role: ActorRole, offset: int = 0, limit: int = 1000
    ) -> WorldView:
        """Build an immutable WorldView for the given actor.

        Applies field-level masking based on role permissions.
        """
        ...

    def execute_command(self, command: Command) -> CommandResult:
        """Validate command against current aggregate state, emit domain events.

        1. Validate command against current aggregates (balance, limits, status).
        2. Generate domain events.
        3. Append events to ChronoDAG store.
        4. Apply events to in-memory aggregates via apply_event handlers.

        Returns the list of emitted domain events.
        Raises ValueError if command violates invariants.
        """
        ...

    def schedule_event(self, event: ScheduledEvent) -> None:
        """Schedule a future discrete-event in the simulation queue."""
        ...

    def get_state_hash(self) -> str:
        """Compute deterministic SHA-256 digest of canonical aggregate state."""
        ...

    def create_account(
        self,
        account_id: str,
        owner_id: str,
        account_type: AccountType,
        initial_balance_paise: int,
        kyc_level: int,
    ) -> None:
        """Genesis-create an account (population bootstrapping, not a Command).

        Unlike execute_command(), there's no TransactionType for account
        creation — but this still goes through the same Emit -> Append to
        ChronoDAG -> Apply pipeline as execute_command(), so genesis events
        are persisted and replay-consistent like any other domain event.
        The caller (population subsystem) generates account_id deterministically.
        """
        ...

    @property
    def sim_time_ns(self) -> float:
        """Current simulation time in nanoseconds (read-only)."""
        ...
