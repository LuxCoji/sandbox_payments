# Subsystem Interface Contracts

This document is the **single source of truth** for all contracts, data shapes, protocol definitions, and boundary rules across FinSim.

---

## 1. Contract Rules & Enforcement

1. **Interface-Only Imports**: Subsystems must interact with each other strictly through `sim/<subsystem>/interfaces.py` and `sim/core/events.py`. Importing concrete implementation modules across subsystem boundaries is strictly forbidden and enforced by `import-linter` in CI.
2. **No Implementation Leaks**: Interface modules contain only dataclasses (mostly frozen), enums, and typing `Protocol` definitions with docstrings.
3. **Immutability & PII Masking**: View objects such as `WorldView` and `AccountSnapshot` are immutable snapshots with role-based field visibility.
4. **State & Event Causality (CQRS)**: In-memory aggregate projections are read-only to external callers and are mutated strictly by applying emitted `DomainEvent` instances via event handlers (`apply_event`).
5. **Types & Units**:
   - Currency: **INR only**, with all amounts represented as integer **paise** (`₹1.00 = 100 paise`).
   - Identifiers: **UUIDv4 strings** (36-character format).
   - Simulation Time: Represented as nanoseconds (`sim_time_ns: float`).

---

## 2. Core Contracts (`sim/core/interfaces.py`)

### Enums

#### `AccountType`
- `PERSONAL`: Retail user account.
- `MERCHANT`: Commercial business account.
- `CASH_ENTITY`: Physical cash endpoint / ATM representation.
- `INTERNAL_SETTLEMENT`: Bank / rail clearing account.
- `ESCROW`: Third-party held funds for pending settlement.

#### `AccountStatus`
- `ACTIVE`: Normal operating state.
- `FROZEN`: Administrative hold; debit/credit blocked.
- `CLOSED`: Terminated account.
- `PENDING_KYC`: Pending identity verification; restricted operations.
- `DISPUTED`: Subject to ongoing dispute or chargeback investigation.

#### `TransactionType`
- `PAYMENT`: User -> Merchant via gateway.
- `TRANSFER`: Account -> Account peer-to-peer.
- `CASH_IN`: Cash Entity -> Account (deposit).
- `CASH_OUT`: Account -> Cash Entity (withdrawal).
- `DEBIT`: Incoming credit (salary, payout).
- `REFUND`: Merchant -> User reversal.
- `CHARGEBACK`: Forced reversal initiated by bank / dispute.
- `SETTLEMENT`: Internal rail batch clearing.
- `FEE`: Rail or gateway charge.
- `INTEREST`: Accrued interest on savings or credit.

#### `PaymentStatus`
- Lifecycle states: `INITIATED`, `AUTHORIZED`, `DECLINED`, `CAPTURED`, `VOIDED`, `SETTLED`, `COMPLETED`, `REFUNDED`, `CHARGED_BACK`.

#### `ActorRole`
- `USER`: Sees own accounts, own devices, public merchant directory, and global parameters.
- `MERCHANT`: Sees own accounts and payer pseudonymous IDs.
- `BANK_OPS`: Sees all accounts (PII masked) and risk scores.
- `RISK_ANALYST`: Sees aggregated graph and model features.
- `RED_AGENT`: Sees only tool responses and public directory data.
- `BLUE_AGENT`: Sees security alerts and case data.

#### `DeviceType` & `DeviceStatus`
- `DeviceType`: `MOBILE`, `POS`, `ATM`, `BROWSER`.
- `DeviceStatus`: `ACTIVE`, `BLOCKED`, `LOST`.

---

### Dataclasses

#### `AccountSnapshot` (frozen)
```python
@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    account_type: AccountType
    balance_paise: int
    status: AccountStatus
    kyc_level: int                           # 0 (none) to 3 (full)
    created_at: float                        # sim_time_ns
    daily_tx_count: int
    daily_tx_volume_paise: int
    linked_device_ids: tuple[str, ...]
    merchant_category_code: str | None = None
```

#### `MerchantDirectoryEntry` (frozen)
```python
@dataclass(frozen=True)
class MerchantDirectoryEntry:
    merchant_id: str
    name: str
    category: str
    avg_rating: float
    settlement_rail: str
```

#### `DeviceSnapshot` (frozen)
```python
@dataclass(frozen=True)
class DeviceSnapshot:
    device_id: str
    owner_id: str
    device_type: DeviceType
    status: DeviceStatus
    registered_at: float
```

#### `FeeSchedule`, `RailLimits`, `GlobalParams` (frozen)
```python
@dataclass(frozen=True)
class FeeSchedule:
    tx_type: TransactionType
    gateway_id: str
    flat_fee_paise: int
    percentage_bps: int
    min_fee_paise: int
    max_fee_paise: int

@dataclass(frozen=True)
class RailLimits:
    rail_id: str
    min_amount_paise: int
    max_amount_paise: int
    daily_limit_paise: int
    cut_off_time_ns: float

@dataclass(frozen=True)
class GlobalParams:
    fee_schedules: tuple[FeeSchedule, ...]
    rail_limits: tuple[RailLimits, ...]
    settlement_cut_off_ns: float
```

#### `WorldView` (frozen)
```python
@dataclass(frozen=True)
class WorldView:
    actor_id: str
    actor_role: ActorRole
    sim_time_ns: float
    accounts: tuple[AccountSnapshot, ...]
    merchants: tuple[MerchantDirectoryEntry, ...]
    devices: tuple[DeviceSnapshot, ...]
    global_params: GlobalParams
```

#### `Command` (frozen)
```python
@dataclass(frozen=True)
class Command:
    command_id: str
    actor_id: str
    action_type: TransactionType
    source_account_id: str | None
    target_account_id: str | None
    amount_paise: int
    idempotency_key: str
    device_id: str | None = None
    gateway_hint: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
```

### `CommandResult` (frozen)
```python
@dataclass(frozen=True)
class CommandResult:
    events: tuple[DomainEvent, ...]
    success: bool
```

---

### Protocol: `WorldEngine`

Defined in `sim/core/interfaces.py`:
- `get_world_view(actor_id: str, actor_role: ActorRole, offset: int = 0, limit: int = 1000) -> WorldView`: Generates role-masked state snapshot.
- `execute_command(command: Command) -> CommandResult`: Validates against in-memory aggregates, generates domain events, and applies events to update state.
- `schedule_event(event: ScheduledEvent) -> None`: Submits a future event to the scheduler.
- `get_state_hash() -> str`: Computes deterministic SHA-256 state hash.
- `sim_time_ns -> float`: Read-only property returning current simulation nanoseconds.

---

## 3. Domain Events (`sim/core/events.py`)

### Envelope: `DomainEvent`
```python
@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    event_type: str
    sim_time_ns: float
    actor_id: str | None
    branch_id: str
    seq_num: int
    causation_id: str | None = None
    correlation_id: str | None = None
```

### Event Types
- **Account Events**: `AccountCreated`, `AccountCredited`, `AccountDebited`, `AccountFrozen`, `AccountClosed`, `AccountStatusChanged`, `KycLevelChanged`, `AccountFreezeFailed`
- **Payment Events**: `PaymentRequested`, `PaymentAuthorized`, `PaymentDeclined`, `PaymentCaptured`, `PaymentSettled`, `PaymentCompleted`, `PaymentRefunded`, `PaymentVoided`, `PaymentChargedBack`, `PaymentTimeout`
- **Device Events**: `DeviceRegistered`, `DeviceStatusChanged`
- **Merchant Events**: `MerchantOnboarded`, `MerchantSuspended`
- **Gateway Events**: `GatewayStatusChanged`
- **Settlement Events**: `SettlementBatchCreated`, `SettlementBatchCompleted`
- **Fee & Interest Events**: `FeeCharged`, `InterestAccrued`
- **Rejection/Counter Events**: `TransferRejected`, `RefundRejected`, `DailyCountersReset`

---

## 4. Population Contracts (`sim/population/interfaces.py`)

### `Intent` (frozen)
```python
@dataclass(frozen=True)
class Intent:
    actor_id: str
    action_type: TransactionType
    target_id: str | None
    amount_paise: int
    idempotency_key: str
    device_id: str | None = None
    gateway_hint: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
```

### `ActionProfile` & `CalibratedParams` (frozen)
```python
@dataclass(frozen=True)
class ActionProfile:
    action_type: TransactionType
    min_count: int
    max_count: int
    avg_amount_paise: int
    std_amount_paise: int
    frequency: float

@dataclass(frozen=True)
class CalibratedParams:
    profiles_by_type: dict[TransactionType, tuple[ActionProfile, ...]]
    initial_balance_distribution: tuple[tuple[int, int, float], ...]
    max_occurrences_per_client: dict[TransactionType, int]
    temporal_rate_matrix: dict[TransactionType, tuple[tuple[float, ...], ...]]
    merchant_category_distribution: dict[str, float]
```

### Protocol: `BehaviourModel`
- `propose_actions(entity_id: str, world_view: WorldView) -> list[Intent]`
- `initialize_entity(entity_id: str, entity_type: str, rng: DeterministicRNG) -> dict[str, object]`
- `get_next_interarrival(entity_id: str, action_type: TransactionType, current_time_ns: float) -> float`

---

## 5. Chrono Contracts (`sim/chrono/interfaces.py`)

### Dataclasses
- `StoredEvent`: Event envelope for persistence in ChronoDAG branch log.
- `Checkpoint`: Captures `checkpoint_id`, `branch_id`, `event_number`, `sim_time_ns`, `state_hash`, `aggregate_snapshot`, `rng_state`.
- `Branch`: Tracks `branch_id`, `parent_checkpoint_id`, `parent_branch_id`, `created_at_ns`, `seed_offset`, `head_seq_num`.
- `ReplayContext`: Contains `branch`, `checkpoint`, `pending_events`.
- `FieldDelta`, `EntityDiff`, `StateDiff`: Recursive delta representation between branches.

### Protocol: `ChronoDAG`
- `save_event(event: StoredEvent) -> None`
- `create_checkpoint(branch_id: str, event_number: int) -> Checkpoint`
- `fork(checkpoint_id: str, branch_id: str, metadata: dict[str, object] | None = None) -> Branch`
- `checkout(branch_id: str) -> ReplayContext`
- `diff(branch_a: str, branch_b: str, at_event: int) -> StateDiff`
- `replay(branch_id: str, from_event: int, to_event: int) -> list[StoredEvent]`
- `get_state_hash(branch_id: str, event_number: int) -> str`

---

## 6. Gateway Contracts (`sim/gateway/interfaces.py`)

### `Capability` Enum & Role Mapping
22 distinct capabilities covering accounts, transactions, devices, merchants, observation, chrono ops, and system operations.

Mapping (`ROLE_CAPABILITIES`):
- `USER`: `VIEW_OWN_ACCOUNT`, `MAKE_PAYMENT`, `TRANSFER_FUNDS`, `REGISTER_DEVICE`, `VIEW_TRANSACTIONS`
- `MERCHANT`: `VIEW_OWN_ACCOUNT`, `REFUND_PAYMENT`, `VIEW_TRANSACTIONS`, `ONBOARD_MERCHANT`
- `BANK_OPS`: `VIEW_ALL_ACCOUNTS`, `FREEZE_ACCOUNT`, `CLOSE_ACCOUNT`, `BLOCK_DEVICE`, `SUSPEND_MERCHANT`, `VIEW_ALL_TRANSACTIONS`, `VIEW_RISK_SCORES`
- `RISK_ANALYST`: `VIEW_ALL_TRANSACTIONS`, `VIEW_RISK_SCORES`, `VIEW_ALERTS`
- `RED_AGENT`: `VIEW_OWN_ACCOUNT`, `MAKE_PAYMENT`, `TRANSFER_FUNDS`, `REGISTER_DEVICE`, `VIEW_TRANSACTIONS`, `CREATE_ACCOUNT`
- `BLUE_AGENT`: `VIEW_ALL_ACCOUNTS`, `VIEW_ALL_TRANSACTIONS`, `VIEW_RISK_SCORES`, `VIEW_ALERTS`, `FREEZE_ACCOUNT`, `BLOCK_DEVICE`, `INITIATE_CHARGEBACK`

### Dataclasses
- `ToolSpec`: `name`, `description`, `required_capabilities`, `parameter_schema`, `rate_limit_per_step`, `rate_limit_per_day`, `visible_fields`.
- `ActorContext`: `actor_id`, `actor_role`, `capabilities`, `branch_id`, `device_id`, `session_id`.
- `ToolResult`: `success`, `tool_name`, `data`, `error_code`, `error_message`, `filtered_fields`.

### Protocol: `ToolGateway`
- `register_tool(spec: ToolSpec) -> None`
- `list_tools(context: ActorContext) -> list[ToolSpec]`
- `call_tool(tool_name: str, parameters: dict[str, object], context: ActorContext) -> ToolResult`

---

## 7. Scheduler & RNG Public Contracts (`sim/scheduler/`)

### `DeterministicRNG` (`sim/scheduler/rng.py`)
- `from_seed(seed: int) -> DeterministicRNG`
- `spawn(*labels: str) -> list[DeterministicRNG]`
- `spawn_for_entity(entity_type: str, entity_id: str) -> DeterministicRNG`
- Sampling: `random`, `uniform`, `normal`, `lognormal`, `poisson`, `exponential`, `integers`, `choice`, `shuffle`
- Snapshotting: `get_state() -> bytes`, `set_state(state: bytes) -> None`

### `SimulationEnv` (`sim/scheduler/env.py`)
- `schedule(event: ScheduledEvent) -> None`
- `step() -> ScheduledEvent | None`
- `run(until: float | None = None) -> int`
- `peek() -> ScheduledEvent | None`
- `pop() -> ScheduledEvent`
- `clear() -> int`
- Properties: `now`, `step_count`, `queue_size`
