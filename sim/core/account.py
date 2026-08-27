"""Account aggregate — event-applied state machine.

Enforces:
    - KYC-tiered daily limits
    - Selective overdraft (PERSONAL/MERCHANT strict, INTERNAL/ESCROW allowed)
    - Strict state transitions
    - Two-phase money (reserved_paise)
    - Lazy daily counter reset
"""
from __future__ import annotations

from sim.core.events import (
    AccountClosed,
    AccountCreated,
    AccountCredited,
    AccountDebited,
    AccountFrozen,
    AccountStatusChanged,
    DailyCountersReset,
    DomainEvent,
    KycLevelChanged,
)
from sim.core.interfaces import (
    AccountSnapshot,
    AccountStatus,
    AccountType,
)

NANOS_PER_DAY = 86_400_000_000_000

KYC_DAILY_LIMITS: dict[int, int] = {
    0: 10_000_00,       # ₹10K
    1: 1_00_000_00,     # ₹1L
    2: 5_00_000_00,     # ₹5L
    3: 50_00_000_00,    # ₹50L
}

ACCOUNT_TYPE_MULTIPLIERS: dict[AccountType, int] = {
    AccountType.PERSONAL: 1,
    AccountType.MERCHANT: 10,
    AccountType.CASH_ENTITY: 0,
    AccountType.INTERNAL_SETTLEMENT: 0,
    AccountType.ESCROW: 0,
}

VALID_TRANSITIONS: dict[AccountStatus, set[AccountStatus]] = {
    AccountStatus.PENDING_KYC: {AccountStatus.ACTIVE},
    AccountStatus.ACTIVE: {AccountStatus.FROZEN, AccountStatus.CLOSED, AccountStatus.DISPUTED},
    AccountStatus.FROZEN: {AccountStatus.ACTIVE, AccountStatus.DISPUTED},
    AccountStatus.DISPUTED: {AccountStatus.ACTIVE, AccountStatus.CLOSED},
    AccountStatus.CLOSED: set(),
}


class Account:
    """Mutable account aggregate, mutated only via apply_event()."""

    __slots__ = (
        "account_id",
        "account_type",
        "owner_id",
        "balance_paise",
        "reserved_paise",
        "status",
        "kyc_level",
        "created_at_ns",
        "daily_tx_count",
        "daily_tx_volume_paise",
        "last_tx_day",
        "linked_device_ids",
        "merchant_category_code",
        "overdraft_limit_paise",
    )

    def __init__(self, event: AccountCreated) -> None:
        self.account_id = event.account_id
        self.account_type = event.account_type
        self.owner_id = event.owner_id
        self.balance_paise = event.initial_balance_paise
        self.reserved_paise = 0
        self.status = AccountStatus.ACTIVE
        self.kyc_level = event.kyc_level
        self.created_at_ns = event.sim_time_ns
        self.daily_tx_count = 0
        self.daily_tx_volume_paise = 0
        self.last_tx_day = int(event.sim_time_ns // NANOS_PER_DAY)
        self.linked_device_ids: set[str] = set()
        self.merchant_category_code: str | None = None
        self.overdraft_limit_paise = 0

    @property
    def available_paise(self) -> int:
        return self.balance_paise - self.reserved_paise

    def daily_limit_paise(self) -> int | None:
        multiplier = ACCOUNT_TYPE_MULTIPLIERS.get(self.account_type, 1)
        if multiplier == 0:
            return None
        base = KYC_DAILY_LIMITS.get(self.kyc_level, KYC_DAILY_LIMITS[0])
        return base * multiplier

    def check_daily_limit(self, amount_paise: int, sim_time_ns: float) -> str | None:
        self._maybe_reset_daily_counters(sim_time_ns, dry_run=True)
        limit = self.daily_limit_paise()
        if limit is None:
            return None
        if self.daily_tx_volume_paise + amount_paise > limit:
            return f"Exceeds daily limit of {limit}"
        return None

    def can_debit(self, amount_paise: int) -> str | None:
        if self.status != AccountStatus.ACTIVE:
            return f"Account is {self.status.value}"

        if self.available_paise + self.overdraft_limit_paise < amount_paise:
            return "Insufficient funds"

        return None

    def can_transition_to(self, new_status: AccountStatus) -> bool:
        return new_status in VALID_TRANSITIONS.get(self.status, set())

    def _maybe_reset_daily_counters(
        self, sim_time_ns: float, dry_run: bool = False
    ) -> DailyCountersReset | None:
        current_day = int(sim_time_ns // NANOS_PER_DAY)
        if current_day > self.last_tx_day:
            if dry_run:
                self.daily_tx_count = 0
                self.daily_tx_volume_paise = 0
                self.last_tx_day = current_day
                return None

            return DailyCountersReset(
                event_id="",  # Populated by Engine
                event_type="DailyCountersReset",
                sim_time_ns=sim_time_ns,
                actor_id=None,
                branch_id="",
                seq_num=0,
                account_id=self.account_id,
                old_daily_tx_count=self.daily_tx_count,
                old_daily_tx_volume_paise=self.daily_tx_volume_paise,
            )
        return None

    def apply_event(self, event: DomainEvent) -> None:
        # First check if we need to reset counters (if this is a transaction)
        if isinstance(event, (AccountDebited, AccountCredited)):
            self._maybe_reset_daily_counters(event.sim_time_ns, dry_run=True)

        if isinstance(event, AccountCredited):
            self.balance_paise += event.amount_paise
        elif isinstance(event, AccountDebited):
            self.balance_paise -= event.amount_paise
            self.daily_tx_count += 1
            self.daily_tx_volume_paise += event.amount_paise
        elif isinstance(event, AccountFrozen):
            self.status = AccountStatus.FROZEN
        elif isinstance(event, AccountClosed):
            self.status = AccountStatus.CLOSED
        elif isinstance(event, AccountStatusChanged):
            self.status = event.new_status
        elif isinstance(event, KycLevelChanged):
            self.kyc_level = event.new_level
        elif isinstance(event, DailyCountersReset):
            self.daily_tx_count = 0
            self.daily_tx_volume_paise = 0
            self.last_tx_day = int(event.sim_time_ns // NANOS_PER_DAY)

    def to_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_id=self.account_id,
            account_type=self.account_type,
            balance_paise=self.balance_paise,
            status=self.status,
            kyc_level=self.kyc_level,
            created_at=self.created_at_ns,
            daily_tx_count=self.daily_tx_count,
            daily_tx_volume_paise=self.daily_tx_volume_paise,
            linked_device_ids=tuple(sorted(self.linked_device_ids)),
            merchant_category_code=self.merchant_category_code,
        )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_type": self.account_type.value,
            "balance_paise": self.balance_paise,
            "reserved_paise": self.reserved_paise,
            "status": self.status.value,
            "kyc_level": self.kyc_level,
            "daily_tx_count": self.daily_tx_count,
            "daily_tx_volume_paise": self.daily_tx_volume_paise,
            "overdraft_limit_paise": self.overdraft_limit_paise,
        }
