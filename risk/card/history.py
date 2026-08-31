"""Turning a stream of single transactions into the sequences the model reads.

This is the piece that does not exist in the offline work and has to exist here.
The card model reads a **trajectory** - what this account has been doing - but a
live payment system hands you one transaction at a time and asks for an answer
before the next one arrives. So something has to remember.

What it remembers is bounded on purpose. Only the last `MAX_SEQ_LEN`
transactions per account are kept, and only accounts seen inside
`RETENTION_NS`. An unbounded store would grow for the length of the simulation
and quietly become the largest object in the process.

## The count and delta features

IEEE-CIS gave the model fourteen `C` columns (counts of things associated with a
card) and fifteen `D` columns (days since various first-seen events). Those were
Vesta's engineered output, but unlike the anonymised `V` block they describe
something any payment system can compute from its own event log - and FinSim
owns its event log.

So they are recomputed here from first principles rather than imported. The
names differ from IEEE-CIS's because the meanings are ours and stating them
plainly is worth more than a false correspondence: `distinct_destinations` is
not `C1`, it is what `C1` was probably measuring.

Every one of these is computed from transactions that arrived **before** the one
being scored. A count that includes the current transaction leaks the present
into a feature that is supposed to describe the past.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from sim.core.interfaces import RiskContext

NANOS_PER_SECOND = 1_000_000_000
NANOS_PER_HOUR = 3_600 * NANOS_PER_SECOND
NANOS_PER_DAY = 24 * NANOS_PER_HOUR

# The model attends over at most this many transactions. Longer histories are
# not more informative here: the attention cost is quadratic and a card's
# recent behaviour is what distinguishes a takeover from normal spending.
MAX_SEQ_LEN = 32

# An account that has not transacted in this long is dropped from the store. It
# will simply look new if it comes back, which is the correct treatment - a
# dormant account resuming activity is itself a signal, not a continuation.
RETENTION_NS = 90 * NANOS_PER_DAY


@dataclass
class Observation:
    """One transaction, reduced to what the model reads.

    Stored rather than the `RiskContext` itself so the history holds a small
    fixed record per transaction instead of pinning a larger object, and so the
    stored shape is explicitly the model's input rather than whatever the engine
    happened to pass.
    """

    sim_time_ns: float
    amount_paise: int
    tx_type: str
    # The account's own attributes, carried on every row rather than looked up
    # separately. They are constant across a sequence, so the model reads them
    # once at position 0 - but keeping them here means a sequence is
    # self-contained and encoding needs no second source.
    account_type: str
    kyc_level: int
    destination_account_id: str
    gateway_id: str
    device_type: str
    time_delta_seconds: float
    hour_of_day: int
    day_of_week: int
    # The count and delta features, as of the moment this transaction arrived.
    distinct_destinations: int
    distinct_devices: int
    distinct_gateways: int
    txns_last_hour: int
    txns_last_day: int
    txns_last_week: int
    seconds_since_first_seen: float
    seconds_since_new_destination: float
    amount_over_account_mean: float


@dataclass
class _AccountState:
    """Everything remembered about one account."""

    first_seen_ns: float
    last_seen_ns: float
    last_new_destination_ns: float
    destinations: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    gateways: set[str] = field(default_factory=set)
    amount_total: int = 0
    amount_count: int = 0
    recent: deque[Observation] = field(default_factory=lambda: deque(maxlen=MAX_SEQ_LEN))
    # Timestamps only, for the rolling windows. Kept separately from `recent`
    # because the windows look further back than the model's attention span.
    #
    # The cap has to exceed what a week can hold, or the counters saturate and
    # stop distinguishing the accounts they exist to separate. At 512 an account
    # transacting once a minute pinned txns_last_day and txns_last_week to the
    # same 512 after eight hours - precisely on a card-testing burst or a
    # high-throughput mule. Eviction below drops anything older than the widest
    # window, so the cap is a safety bound rather than the real limit.
    timestamps: deque[float] = field(default_factory=lambda: deque(maxlen=20_000))


class AccountHistory:
    """Per-account transaction history, bounded in both length and age.

    Single-threaded by design, matching the simulation engine. Concurrency would
    need locking per account, and the simulator has no concurrency to protect
    against.
    """

    def __init__(self, max_seq_len: int = MAX_SEQ_LEN,
                 retention_ns: float = RETENTION_NS) -> None:
        self._max_seq_len = max_seq_len
        self._retention_ns = retention_ns
        self._accounts: dict[str, _AccountState] = {}

    def __len__(self) -> int:
        return len(self._accounts)

    def observe(self, context: RiskContext) -> Observation:
        """Record a transaction and return it as the model sees it.

        The returned observation describes the account **as it was before this
        transaction**. The state is updated afterwards, so a count never
        includes the row it is describing.
        """
        account_id = context.source_account_id
        state = self._accounts.get(account_id)

        if state is None:
            state = _AccountState(
                first_seen_ns=context.sim_time_ns,
                last_seen_ns=context.sim_time_ns,
                last_new_destination_ns=context.sim_time_ns,
            )
            self._accounts[account_id] = state
            # A first transaction has no predecessor. -1 marks that rather than
            # 0, which would read as "instantaneous" and is a real pattern.
            time_delta = -1.0
        else:
            time_delta = (context.sim_time_ns - state.last_seen_ns) / NANOS_PER_SECOND

        observation = Observation(
            sim_time_ns=context.sim_time_ns,
            amount_paise=context.amount_paise,
            tx_type=context.tx_type.value,
            account_type=(context.source_account_type.value
                          if context.source_account_type else "unknown"),
            kyc_level=context.source_kyc_level,
            destination_account_id=context.destination_account_id,
            gateway_id=context.gateway_id or "none",
            device_type=context.device_type.value if context.device_type else "none",
            time_delta_seconds=time_delta,
            hour_of_day=_hour_of_day(context.sim_time_ns),
            day_of_week=_day_of_week(context.sim_time_ns),
            distinct_destinations=len(state.destinations),
            distinct_devices=len(state.devices),
            distinct_gateways=len(state.gateways),
            txns_last_hour=_count_within(state.timestamps, context.sim_time_ns, NANOS_PER_HOUR),
            txns_last_day=_count_within(state.timestamps, context.sim_time_ns, NANOS_PER_DAY),
            txns_last_week=_count_within(state.timestamps, context.sim_time_ns, 7 * NANOS_PER_DAY),
            seconds_since_first_seen=(context.sim_time_ns - state.first_seen_ns) / NANOS_PER_SECOND,
            seconds_since_new_destination=(
                context.sim_time_ns - state.last_new_destination_ns) / NANOS_PER_SECOND,
            amount_over_account_mean=_ratio_to_mean(
                context.amount_paise, state.amount_total, state.amount_count),
        )

        self._advance(state, context, observation)
        return observation

    def sequence(self, account_id: str) -> list[Observation]:
        """This account's recent history, oldest first. Empty if unknown."""
        state = self._accounts.get(account_id)
        return list(state.recent) if state else []

    def evict_stale(self, now_ns: float) -> int:
        """Drop accounts untouched for longer than the retention window.

        Called by the engine on a schedule rather than on every transaction: a
        sweep over every account on every payment would make scoring cost grow
        with the size of the simulation.
        """
        cutoff = now_ns - self._retention_ns
        stale = [k for k, v in self._accounts.items() if v.last_seen_ns < cutoff]
        for key in stale:
            del self._accounts[key]
        return len(stale)

    def _advance(self, state: _AccountState, context: RiskContext,
                 observation: Observation) -> None:
        """Fold this transaction into the account's state. Called last."""
        if context.destination_account_id and context.destination_account_id not in state.destinations:
            state.destinations.add(context.destination_account_id)
            state.last_new_destination_ns = context.sim_time_ns
        if observation.device_type != "none":
            state.devices.add(observation.device_type)
        if observation.gateway_id != "none":
            state.gateways.add(observation.gateway_id)

        state.amount_total += context.amount_paise
        state.amount_count += 1
        state.last_seen_ns = context.sim_time_ns
        state.timestamps.append(context.sim_time_ns)
        # Anything older than the widest rolling window can never be counted
        # again, so it is dropped here rather than left to the deque's cap.
        oldest = context.sim_time_ns - 7 * NANOS_PER_DAY
        while state.timestamps and state.timestamps[0] < oldest:
            state.timestamps.popleft()
        state.recent.append(observation)


def _count_within(timestamps: deque[float], now_ns: float, window_ns: float) -> int:
    """How many of the stored timestamps fall inside the window ending now.

    Walks backwards and stops at the first one outside the window, because the
    deque is append-ordered and therefore already sorted.
    """
    cutoff = now_ns - window_ns
    count = 0
    for stamp in reversed(timestamps):
        if stamp < cutoff:
            break
        count += 1
    return count


def _ratio_to_mean(amount_paise: int, total: int, count: int) -> float:
    """This amount against the account's own average, before this transaction.

    Returns 1.0 for a first transaction. An account with no history has no
    average to be unusual against, and inventing a large ratio there would make
    every new account look suspicious.
    """
    if count == 0 or total == 0:
        return 1.0
    return amount_paise / (total / count)


def _hour_of_day(sim_time_ns: float) -> int:
    return int((sim_time_ns // NANOS_PER_HOUR) % 24)


def _day_of_week(sim_time_ns: float) -> int:
    return int((sim_time_ns // NANOS_PER_DAY) % 7)
