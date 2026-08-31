"""Tests for the fraud rails.

Two things are being checked, and they are different in kind. Some tests assert
rules that must hold whatever the model says - the wire rail never blocks, the
engine is unchanged with no scorer wired. Others assert that the structural
detection actually fires on a laundering shape, which is a claim about the
implementation rather than about accuracy.

Nothing here claims a detection rate. No model has been trained on this
simulator's traffic yet, and a test cannot manufacture one.
"""
from __future__ import annotations

import pytest

from risk.card.history import NANOS_PER_DAY, NANOS_PER_HOUR, AccountHistory
from risk.engine import FraudRiskEngine
from risk.thresholds import AmountAwareBands
from risk.wire.graph import TransferGraph
from risk.wire.scorer import WireScorer
from sim.core.interfaces import (
    AccountType,
    DeviceType,
    RiskAction,
    RiskContext,
    TransactionType,
)


def context(source: str, destination: str, amount_paise: int, at_ns: float,
            tx_type: TransactionType = TransactionType.TRANSFER,
            tx_id: str = "tx") -> RiskContext:
    return RiskContext(
        tx_id=tx_id, tx_type=tx_type, actor_id=f"owner-{source}",
        source_account_id=source, destination_account_id=destination,
        amount_paise=amount_paise, sim_time_ns=at_ns,
        gateway_id="gw1", device_type=DeviceType.MOBILE,
        source_account_type=AccountType.PERSONAL, source_kyc_level=1,
    )


# --- rules that hold regardless of any model ---------------------------------

def test_the_wire_rail_never_blocks_a_transfer():
    """Laundering detection runs at roughly 12% precision.

    Blocking automatically would refuse about eight innocent transfers for every
    real laundering leg. It is also how a bank commits the offence of tipping
    off: a decline that says "flagged as suspicious" tells the customer they are
    under review. Both reasons are permanent, so this is asserted over a shape
    built specifically to light up every signal at once.
    """
    scorer = WireScorer()
    now = 0.0
    # A hub that receives from many, forwards nearly everything, and closes a
    # loop - as suspicious as this rail can see.
    for i in range(12):
        scorer.assess(context(f"payer{i}", "hub", 500_000, now + i * NANOS_PER_HOUR / 4))
    for i in range(12):
        scorer.assess(context("hub", f"mule{i}", 480_000, now + (i + 12) * NANOS_PER_HOUR / 4))

    decisions = [scorer.assess(context("hub", f"mule{i}", 400_000,
                                       now + 8 * NANOS_PER_HOUR))
                 for i in range(5)]

    assert all(d.action is not RiskAction.BLOCK for d in decisions)
    assert any(d.action is RiskAction.REVIEW for d in decisions), (
        "this shape should raise a case even though it must not block one")


def world_engine(risk=None):
    """A simulation engine with two funded accounts, ready to move money."""
    from sim.core.engine import WorldEngineImpl
    from sim.core.events import AccountCreated
    from sim.scheduler.env import SimulationEnv
    from sim.scheduler.rng import DeterministicRNG

    engine = WorldEngineImpl(env=SimulationEnv(), rng=DeterministicRNG.from_seed(1),
                             risk=risk)
    for i, (account, owner, balance) in enumerate(
            [("acc1", "user1", 1_000_000), ("acc2", "user2", 0)], start=1):
        engine._apply_event(AccountCreated(
            event_id=f"e{i}", event_type="AccountCreated", sim_time_ns=0,
            actor_id="sys", branch_id="main", seq_num=i, account_id=account,
            account_type=AccountType.PERSONAL, initial_balance_paise=balance,
            kyc_level=1, owner_id=owner))
    return engine


def pay(engine, amount_paise=1000, key="ik1"):
    from sim.core.interfaces import Command

    return engine.execute_command(Command(
        command_id="c1", actor_id="user1", action_type=TransactionType.PAYMENT,
        source_account_id="acc1", target_account_id="acc2",
        amount_paise=amount_paise, idempotency_key=key))


def test_an_engine_with_no_scorer_behaves_exactly_as_before():
    """The seam must be invisible when nothing is wired into it.

    Every existing replay, determinism and state-hash test in the simulation
    depends on the engine emitting what it always emitted. If injecting the
    protocol changed behaviour by default, those tests would silently start
    asserting the new behaviour and the old guarantee would be gone.
    """
    result = pay(world_engine(risk=None))

    assert result.success
    assert [e.event_type for e in result.events] == ["PaymentRequested",
                                                     "PaymentAuthorized"]


def test_a_scorer_that_blocks_declines_the_payment():
    """The card rail is allowed to stop a payment, and this is how it does it."""
    from sim.core.interfaces import RiskDecision

    class AlwaysBlock:
        def assess(self, context):
            return RiskDecision(action=RiskAction.BLOCK, score=0.99,
                                rail="card", reason="stolen card")

    result = pay(world_engine(risk=AlwaysBlock()))

    assert not result.success
    declined = result.events[-1]
    assert declined.event_type == "PaymentDeclined"
    assert declined.decline_code == "RISK_BLOCKED"
    assert declined.reason == "stolen card"


def test_risk_is_consulted_only_after_ownership_and_funds():
    """A payment that was going to fail anyway must never reach the model.

    Two reasons. It would teach the model that "declined for no money" looks
    like fraud, and it would let an attacker probe the risk system for free with
    payments they cannot fund.
    """
    from sim.core.interfaces import RiskDecision

    class Recording:
        def __init__(self):
            self.calls = 0

        def assess(self, context):
            self.calls += 1
            return RiskDecision.allow(rail="card")

    scorer = Recording()
    engine = world_engine(risk=scorer)
    result = pay(engine, amount_paise=999_999_999, key="broke")

    assert not result.success
    assert result.events[-1].decline_code == "INSUFFICIENT_FUNDS"
    assert scorer.calls == 0, "an unfundable payment was scored"


def test_unscored_transaction_types_are_left_alone():
    """Cash in, fees and interest are not routes an attacker controls."""
    engine = FraudRiskEngine()
    for tx_type in (TransactionType.CASH_IN, TransactionType.FEE,
                    TransactionType.INTEREST, TransactionType.SETTLEMENT):
        decision = engine.assess(context("a", "b", 10_000, 0.0, tx_type))
        assert decision.action is RiskAction.ALLOW

    assert engine.counters.unscored == 4
    assert engine.summary()["scored"] == 0


def test_a_failing_scorer_does_not_stop_payments():
    """A broken model must not become an outage that declines everything.

    A risk model is advisory. If it crashes, payments have to keep clearing -
    an exception escaping into the command pipeline would turn one bad model
    file into a total payments outage, which is a far worse failure than
    missing some fraud.
    """
    class Exploding:
        def assess(self, context):
            raise RuntimeError("model file is corrupt")

    result = pay(world_engine(risk=Exploding()))

    assert result.success, "a crashing model stopped a legitimate payment"
    assert result.events[-1].event_type == "PaymentAuthorized"


# --- the structural detection actually fires ---------------------------------

def test_a_mule_chain_is_flagged():
    """Money in, money straight back out, to accounts never paid before.

    This is the mule primitive the red-team playbook describes: a collection
    account that forwards nearly everything it receives. Nothing about any
    single transfer is wrong, which is exactly why it needs the graph.
    """
    scorer = WireScorer()
    at = 0.0
    for i in range(10):
        scorer.assess(context(f"victim{i}", "mule", 200_000, at + i * 60e9))

    at += 3600e9
    decision = None
    for i in range(10):
        decision = scorer.assess(context("mule", f"cashout{i}", 195_000,
                                         at + i * 60e9))

    assert decision.action is RiskAction.REVIEW
    assert "forwarded" in decision.reason or "paid" in decision.reason


def test_a_slow_loop_is_not_a_tight_cycle():
    """Ordinary business forms loops. Speed is what separates them.

    An earlier version of the offline rail used a thirty-day window on an
    eighteen-day dataset, so every cycle trivially passed: 372,952 survived and
    71.6% of accounts were flagged. The window is the filter.
    """
    graph = TransferGraph()
    # A -> B -> C -> A, but spread over a fortnight.
    graph.add("A", "B", 100_000, 0.0)
    graph.add("B", "C", 100_000, 5 * NANOS_PER_DAY)
    graph.add("C", "A", 100_000, 12 * NANOS_PER_DAY)

    structure = graph.structure("A", 12 * NANOS_PER_DAY)
    assert structure.cycle_count == 0, "a loop closing over days is not a tight cycle"


def test_a_fast_loop_is_a_tight_cycle():
    graph = TransferGraph()
    graph.add("A", "B", 100_000, 0.0)
    graph.add("B", "C", 100_000, 2 * NANOS_PER_HOUR)
    graph.add("C", "A", 100_000, 5 * NANOS_PER_HOUR)

    structure = graph.structure("A", 5 * NANOS_PER_HOUR)
    assert structure.cycle_count == 1
    assert structure.shortest_cycle == 3
    assert 0 < structure.fastest_cycle_hours <= 24


def test_the_graph_forgets_what_falls_out_of_its_window():
    """Unbounded growth is the failure that only appears in a long run."""
    graph = TransferGraph()
    for i in range(20):
        graph.add(f"a{i}", f"b{i}", 1_000, i * NANOS_PER_DAY)

    # The window is seven days, so only the last week of edges survives.
    assert len(graph) <= 8, f"graph kept {len(graph)} edges outside its window"


# --- the card history -------------------------------------------------------

def test_history_features_describe_the_past_not_the_present():
    """A count that includes the row it describes leaks the present.

    The first transaction to a new destination must report the destination
    count as it was *before* that transaction, otherwise every first payment
    looks like an account with history.
    """
    history = AccountHistory()
    first = history.observe(context("acct", "shop1", 10_000, 0.0,
                                    TransactionType.PAYMENT))
    assert first.distinct_destinations == 0, "the first payment has no prior destinations"
    assert first.time_delta_seconds == -1.0, "a first transaction has no predecessor"
    assert first.amount_over_account_mean == 1.0, "no history means no ratio to be unusual against"

    second = history.observe(context("acct", "shop2", 10_000, 3600e9,
                                     TransactionType.PAYMENT))
    assert second.distinct_destinations == 1
    assert second.time_delta_seconds == pytest.approx(3600.0)


def test_history_is_bounded_per_account():
    history = AccountHistory()
    for i in range(200):
        history.observe(context("acct", f"shop{i}", 10_000, i * 60e9,
                                TransactionType.PAYMENT))

    sequence = history.sequence("acct")
    assert len(sequence) == 32, "the model attends over a bounded window"
    assert sequence[-1].destination_account_id == "shop199", "the newest is kept"


def test_stale_accounts_are_evicted():
    history = AccountHistory()
    history.observe(context("old", "x", 1_000, 0.0, TransactionType.PAYMENT))
    history.observe(context("new", "x", 1_000, 100 * NANOS_PER_DAY,
                            TransactionType.PAYMENT))

    dropped = history.evict_stale(100 * NANOS_PER_DAY)
    assert dropped == 1
    assert history.sequence("old") == []
    assert len(history.sequence("new")) == 1


# --- thresholds -------------------------------------------------------------

def test_a_large_payment_faces_a_lower_bar():
    """Elkan's rule: the cost of missing fraud is the amount at risk.

    Being wrong about a fifty-rupee payment costs a customer ten seconds. Being
    wrong about a five-lakh payment costs five lakh. The threshold has to move.
    """
    bands = AmountAwareBands()
    small = bands.step_up_threshold(5_000)          # 50 rupees
    large = bands.step_up_threshold(50_000_000)     # 5 lakh

    assert small > large, "a small payment should need more confidence, not less"
    assert 0 < large < small <= 0.99


def test_blocking_always_needs_at_least_as_much_confidence_as_challenging():
    bands = AmountAwareBands()
    for amount in (1, 5_000, 100_000, 10_000_000, 1_000_000_000):
        assert bands.block_threshold(amount) >= bands.step_up_threshold(amount)


def test_thresholds_stay_inside_their_bounds():
    """Both bounds matter, and card testing is why the ceiling does.

    Without a floor, a large enough amount drives the threshold to zero and
    every payment of that size is stopped regardless of score. Without a
    ceiling, a one-rupee charge is never stopped - which is exactly how a
    fraudster validates a stolen card before using it.
    """
    bands = AmountAwareBands()
    assert bands.step_up_threshold(10**12) >= bands.floor
    assert bands.step_up_threshold(1) <= bands.ceiling
