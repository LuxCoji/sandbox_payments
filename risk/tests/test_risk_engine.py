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

from pathlib import Path

import numpy as np
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


# --- the freeze workflow ----------------------------------------------------

def a_case(tx_id="tx1", amount_paise=500_000):
    from risk.engine import Case

    return Case(tx_id=tx_id, rail="wire", action="REVIEW", score=0.8,
                reason="forwarded 96% of what it received",
                amount_paise=amount_paise, source_account_id="mule",
                destination_account_id="cashout", sim_time_ns=0.0)


def test_a_freeze_requires_a_named_reviewer(tmp_path):
    """"Who decided this" is the first question an audit asks.

    Defaulting to "system" would make every freeze unattributable, which is the
    same as having no audit trail at all.
    """
    from risk.actions import CaseLog, request_freeze

    log = CaseLog(tmp_path / "cases.jsonl")
    with pytest.raises(ValueError):
        request_freeze(a_case(), reviewer="  ", reason="mule chain", log=log)
    with pytest.raises(ValueError):
        request_freeze(a_case(), reviewer="anita", reason="", log=log)

    decision = request_freeze(a_case(), reviewer="anita",
                              reason="mule chain", log=log)
    assert decision.reviewer == "anita"


def test_a_large_freeze_needs_a_second_approval(tmp_path):
    """Large freezes are the most damaging when wrong, so one person is not enough."""
    from risk.actions import DUAL_APPROVAL_ABOVE_PAISE, CaseLog, request_freeze

    log = CaseLog(tmp_path / "cases.jsonl")
    big = request_freeze(a_case("big", DUAL_APPROVAL_ABOVE_PAISE + 1),
                         reviewer="anita", reason="layering", log=log)
    assert big.pending, "a freeze above the threshold must wait for a second reviewer"

    approved = request_freeze(a_case("big2", DUAL_APPROVAL_ABOVE_PAISE + 1),
                              reviewer="anita", reason="layering", log=log,
                              second_reviewer="ravi")
    assert not approved.pending

    small = request_freeze(a_case("small", 1_000), reviewer="anita",
                           reason="mule", log=log)
    assert not small.pending


def test_the_log_is_append_only_and_reconstructible(tmp_path):
    """A cleared case that turns out to be laundering has to be explainable."""
    from risk.actions import CaseLog, clear_case, reopen_case, request_freeze

    log = CaseLog(tmp_path / "cases.jsonl")
    clear_case(a_case(), reviewer="anita", reason="known supplier", log=log)
    assert log.current_state("tx1") == "cleared"

    reopen_case("tx1", reviewer="ravi", reason="new pattern", log=log)
    assert log.current_state("tx1") == "reopened"

    request_freeze(a_case(), reviewer="ravi", reason="confirmed", log=log)
    assert log.current_state("tx1") == "freeze_requested"

    # Nothing was overwritten: the whole sequence is still there.
    history = [d.action for d in log.for_case("tx1")]
    assert history == ["cleared", "reopened", "freeze_requested"]


def test_the_wire_rail_never_notifies_the_customer():
    """Telling someone they are under AML review is a criminal offence.

    Card fraud is the opposite: a customer who is not told why their payment
    was declined will simply call support.
    """
    from risk.actions import notify_customer

    wire = notify_customer("wire", "freeze_requested")
    assert wire["notify"] is False
    assert "tipping off" in wire["reason"]

    card = notify_customer("card", "STEP_UP")
    assert card["notify"] is True
    assert card["message"]


def test_decided_cases_leave_the_queue(tmp_path):
    from risk.actions import CaseLog, clear_case, open_cases

    log = CaseLog(tmp_path / "cases.jsonl")
    cases = [a_case("tx1"), a_case("tx2"), a_case("tx3")]
    assert len(open_cases(cases, log)) == 3

    clear_case(cases[1], reviewer="anita", reason="fine", log=log)
    remaining = [c.tx_id for c in open_cases(cases, log)]
    assert remaining == ["tx1", "tx3"]


# --- collecting training data -----------------------------------------------

def test_traffic_is_recorded_with_exact_labels(tmp_path):
    """Ground truth comes from who acted, not from a heuristic.

    A recorder that worked out for itself who the attacker was would be a fraud
    model, and using one model's guesses as another model's labels is how a
    system learns to agree with itself.
    """
    import json

    from risk.collect import TrafficRecorder

    path = tmp_path / "traffic.jsonl"
    recorder = TrafficRecorder(path, attacker_actor_ids={"owner-attacker"})
    engine = FraudRiskEngine(recorder=recorder)

    engine.assess(context("honest", "shop", 10_000, 0.0, TransactionType.PAYMENT))
    engine.assess(context("attacker", "shop", 10_000, 60e9, TransactionType.PAYMENT))
    recorder.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert [r["is_fraud"] for r in rows] == [0, 1]
    assert rows[0]["observation"]["amount_paise"] == 10_000


def test_recorded_rows_rebuild_into_trainable_sequences(tmp_path):
    """What the recorder writes must be exactly what training reads.

    Training and serving building the feature vector differently is a silent
    failure - no exception, just worse predictions that look like a weak model.
    This asserts the round trip.
    """
    import json

    from risk.card.encoding import Vocabulary, encode_sequence
    from risk.card.training import to_sequences
    from risk.collect import TrafficRecorder

    path = tmp_path / "traffic.jsonl"
    recorder = TrafficRecorder(path, attacker_actor_ids={"owner-bad"})
    engine = FraudRiskEngine(recorder=recorder)
    for i in range(5):
        engine.assess(context("acct", f"shop{i}", 10_000 * (i + 1), i * 60e9,
                              TransactionType.PAYMENT))
    recorder.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    sequences, labels = to_sequences(rows)

    assert len(sequences) == 5
    assert [len(s) for s in sequences] == [1, 2, 3, 4, 5], (
        "each row is the account's history up to and including that transaction")

    vocab = Vocabulary.fit(sequences)
    arrays = encode_sequence(sequences[-1], vocab)
    assert arrays["length"] == 5
    assert arrays["pad_mask"][0, :5].tolist() == [False] * 5
    assert arrays["pad_mask"][0, 5:].all(), "the tail must stay padded"


# --- the model path, end to end ---------------------------------------------

def test_an_empty_history_passed_in_is_the_one_used():
    """`or` on a container that defines __len__ silently discards it.

    `self.history = history or AccountHistory()` looks harmless and is not: an
    empty AccountHistory is falsy, so the caller's object was thrown away and a
    second one built. The engine and its card scorer then kept separate
    histories, the scorer's sequences never reached the recorder, and the
    training set came out empty - with no error anywhere.
    """
    shared = AccountHistory()
    engine = FraudRiskEngine(history=shared)

    assert engine.history is shared
    assert engine.card.history is shared

    engine.assess(context("acct", "shop", 10_000, 0.0, TransactionType.PAYMENT))
    assert len(shared) == 1, "the transaction went into a different history"


def test_a_trained_model_saves_loads_and_scores(tmp_path):
    """The whole path: collect, train, save, load, score one payment.

    Deliberately tiny - two layers, one epoch, a hundred transactions. It proves
    nothing about accuracy and everything about the plumbing: that what the
    recorder writes is what training reads, that a checkpoint carries its own
    vocabulary and shape, and that a loaded model plugs into the scorer without
    anything else changing.
    """
    import json

    from risk.card.model import TorchSequenceModel
    from risk.card.scorer import SequenceCardScorer
    from risk.card.training import train
    from risk.collect import TrafficRecorder

    traffic = tmp_path / "traffic.jsonl"
    recorder = TrafficRecorder(traffic, attacker_actor_ids={"owner-bad"})
    collecting = FraudRiskEngine(recorder=recorder)

    for i in range(100):
        account = "bad" if i % 10 == 0 else f"good{i % 7}"
        collecting.assess(context(account, f"shop{i % 5}", 10_000 + i * 100,
                                  i * 60e9, TransactionType.PAYMENT))
    recorder.close()

    rows = [json.loads(line) for line in traffic.read_text().splitlines()]
    assert sum(r["is_fraud"] for r in rows) > 0, "the fixture produced no fraud"

    out = tmp_path / "card.pt"
    result = train(traffic, out, pretrain_epochs=1, finetune_epochs=1,
                   model_config={"d_model": 32, "n_layers": 2, "n_heads": 2,
                                 "input_layers": 1, "max_seq_len": 32})
    assert out.exists()
    assert 0.0 <= result["recall_at_2pct"] <= 1.0

    # The part that matters: loading it back and scoring a live payment.
    model = TorchSequenceModel.load(out)
    scorer = SequenceCardScorer(model=model)
    engine = FraudRiskEngine(card_scorer=scorer, history=scorer.history)

    decision = engine.assess(context("someone", "shop", 25_000, 0.0,
                                     TransactionType.PAYMENT))
    assert decision.rail == "card"
    assert 0.0 <= decision.score <= 1.0
    assert decision.action in (RiskAction.ALLOW, RiskAction.STEP_UP,
                               RiskAction.BLOCK)


def test_loading_a_missing_model_fails_loudly(tmp_path):
    """Silently falling back to "allow everything" is how a rail dies unnoticed."""
    from risk.card.model import TorchSequenceModel

    with pytest.raises(FileNotFoundError, match="no card model"):
        TorchSequenceModel.load(tmp_path / "absent.pt")


# --- the wire rail's thresholds ---------------------------------------------

def test_passthrough_is_a_band_not_a_floor():
    """Sending far more than you received is not the pass-through pattern.

    `passthrough` is sent/received and unbounded. An ordinary account spending
    money that arrived from outside the transfer graph - a salary, a deposit, a
    card refund - reaches ratios well above 1.0. A rule reading "at least 0.90"
    therefore fired on ordinary behaviour: measured on legitimate simulator
    traffic it flagged 80% of honest accounts, with reasons like "forwarded 246%
    of what it received".

    A mule sits just *below* one: money arrives and nearly all of it leaves.
    """
    from risk.wire.scorer import WireThresholds

    limits = WireThresholds()
    scorer = WireScorer(thresholds=limits)

    def passthrough_signal(sent, received):
        graph = TransferGraph()
        graph.add("payer", "account", received, 0.0)
        graph.add("account", "payee", sent, 60e9)
        structure = graph.structure("account", 60e9)
        names = {s.name for s in scorer._signals(structure, "account")}
        return "passthrough" in names

    assert passthrough_signal(sent=950_000, received=1_000_000), (
        "a mule forwarding 95% of what it received must be flagged")
    assert not passthrough_signal(sent=2_460_000, received=1_000_000), (
        "spending money from outside the graph is not pass-through")
    assert not passthrough_signal(sent=100_000, received=1_000_000), (
        "keeping 90% of what arrived is ordinary saving")


def test_the_review_bar_can_be_fitted_to_a_flag_rate():
    """A hand-picked bar is a guess; a fitted one spends a known budget.

    The original 0.60 was reasoned about rather than measured, and a textbook
    mule chain - six accounts in, 93% straight back out to six others within the
    hour - scored 0.35 and passed unflagged. The fix is not a lower guess, it is
    fitting the bar to the distribution of real traffic.
    """
    from risk.wire.scorer import calibrate

    # Ordinary traffic: everyone pays a couple of people, keeps most of it.
    transfers = []
    at = 0.0
    for i in range(400):
        at += 300e9
        transfers.append(context(f"person{i % 90}", f"person{(i * 7) % 90}",
                                 50_000, at))

    fitted = calibrate(transfers, target_flag_rate=0.02)
    scorer = WireScorer(thresholds=fitted)
    flagged = sum(1 for t in transfers
                  if WireScorer(thresholds=fitted).assess(t).action is RiskAction.REVIEW)

    assert flagged <= len(transfers) * 0.05, (
        f"fitted bar flagged {flagged} of {len(transfers)} - the budget was 2%")


def test_calibration_refuses_an_empty_sample():
    """Fitting a threshold on nothing would return the default and look fitted."""
    from risk.wire.scorer import calibrate

    with pytest.raises(ValueError):
        calibrate([])


# --- drift monitoring -------------------------------------------------------

def test_drift_is_measured_against_stored_bins():
    """Recomputed bins move with the data and hide the drift they exist to find.

    This is the failure the wire rail's own history warns about: a threshold or
    a bin edge that is re-derived from the live window adapts to whatever
    arrives, so a distribution that has shifted lands in shifted bins and the
    monitor reports everything is fine.
    """
    import numpy as np

    from risk.monitoring import Reference, check

    rng = np.random.default_rng(0)
    reference = Reference.fit(rng.beta(2, 20, 5000), threshold=0.30)

    assert check(reference, rng.beta(2, 20, 3000)).healthy, (
        "the same distribution must not raise drift")
    assert not check(reference, rng.beta(5, 10, 3000)).healthy, (
        "a shifted distribution must raise drift")


def test_an_empty_bin_is_floored_not_skipped():
    """A bin that held 8% and now holds nothing is the strongest drift signal."""
    import numpy as np

    from risk.monitoring import Reference, population_stability_index

    rng = np.random.default_rng(1)
    reference = Reference.fit(rng.uniform(0, 1, 4000), threshold=0.5)
    # Everything collapses into the bottom bin: most reference bins go empty.
    psi = population_stability_index(reference, np.full(2000, 0.01))

    assert np.isfinite(psi), "empty bins produced a non-finite PSI"
    assert psi > 1.0, f"a total collapse should be obvious drift, got {psi:.3f}"


def test_a_reference_needs_enough_scores_to_be_a_distribution():
    import numpy as np

    from risk.monitoring import Reference

    with pytest.raises(ValueError):
        Reference.fit(np.array([0.1, 0.2, 0.3]), threshold=0.5)


# --- the console ------------------------------------------------------------

def test_the_console_says_when_the_card_rail_is_untrained():
    """An empty case list looks identical whether the rail is clean or off."""
    from risk.console import render

    page = render({"scored": 100, "flagged": 0, "flag_rate": 0.0, "blocked": 0,
                   "review": 0, "accounts_tracked": 10},
                  cases=[], card_model_loaded=False)
    assert "no trained model" in page.lower()

    page = render({"scored": 100, "flagged": 0, "flag_rate": 0.0, "blocked": 0,
                   "review": 0, "accounts_tracked": 10},
                  cases=[], card_model_loaded=True)
    assert "no trained model" not in page.lower()


def test_the_console_escapes_everything_it_renders():
    """Case reasons carry account ids and model output, none of it trusted."""
    from risk.console import render
    from risk.engine import Case

    nasty = Case(tx_id="<script>alert(1)</script>", rail="wire", action="REVIEW",
                 score=0.9, reason="<img src=x onerror=alert(1)>",
                 amount_paise=100, source_account_id="a",
                 destination_account_id="b", sim_time_ns=0.0)
    page = render({"scored": 1}, [nasty], card_model_loaded=True)

    # What matters is that no tag delimiter from the data survives as markup.
    # The words "onerror" and "script" *do* survive as escaped text, which is
    # harmless - asserting their absence would be testing the wrong thing and
    # would fail on a correct escaper.
    assert "&lt;script&gt;" in page, "the tag was not escaped"
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    body = page[page.index("<table>"):]
    assert "<script" not in body and "<img" not in body, (
        "a tag from the case data reached the document as markup")


# --- bugs a review found, each pinned ---------------------------------------

def _row(i, fraud):
    return {"account_id": "a", "is_fraud": int(fraud), "observation": {
        "sim_time_ns": i * 60e9, "amount_paise": 1000, "tx_type": "PAYMENT",
        "account_type": "PERSONAL", "kyc_level": 1,
        "destination_account_id": "d", "gateway_id": "g",
        "device_type": "MOBILE", "time_delta_seconds": 60.0,
        "hour_of_day": i % 24, "day_of_week": i % 7,
        "distinct_destinations": 1, "distinct_devices": 1,
        "distinct_gateways": 1, "txns_last_hour": 1, "txns_last_day": 1,
        "txns_last_week": 1, "seconds_since_first_seen": 60.0 * i,
        "seconds_since_new_destination": 60.0,
        "amount_over_account_mean": 1.0}}


def test_the_loss_only_reads_positions_that_carry_a_label():
    """A row labelled fraud must not also be presented to the loss as genuine.

    `to_sequences` emits one sequence per transaction, so a fraudulent row
    reappears as a non-final position in up to 31 later sequences. Those
    positions sit at a placeholder zero, and a loss reading `~pad_mask` saw the
    fraud once labelled 1 and twenty-nine times labelled 0 - training the model
    against itself on exactly the rows that matter most.
    """
    from risk.card.encoding import Vocabulary
    from risk.card.training import stack, to_sequences

    rows = [_row(i, i == 20) for i in range(40)]
    sequences, labels = to_sequences(rows)
    arrays = stack(sequences, labels, Vocabulary.fit(sequences))

    mask = arrays["label_mask"]
    assert mask.sum() == len(sequences), "one labelled position per sequence"
    assert (mask.sum(axis=1) == 1).all(), "exactly one, never more"

    # The label must land on the position serving reads: the last real one.
    lengths = (~arrays["pad_mask"]).sum(axis=1)
    assert np.array_equal(mask.argmax(axis=1), lengths - 1)
    assert int((arrays["sig_cat"][..., 0] * mask).sum()) == int(labels.sum())


def test_the_positive_weight_counts_labelled_positions_only():
    """Weighting over every real position understates the fraud rate thirtyfold."""
    from risk.card.treasure.train import positive_weight

    n, length = 100, 32
    seq = {"sig_cat": np.zeros((n, length, 1), dtype="int64"),
           "pad_mask": np.zeros((n, length), dtype=bool),
           "label_mask": np.zeros((n, length), dtype=bool)}
    seq["label_mask"][:, -1] = True
    seq["sig_cat"][:10, -1, 0] = 1          # 10% of the labelled positions

    assert positive_weight(seq) == pytest.approx(9.0, rel=0.01)


def test_an_instantaneous_cycle_is_the_strongest_signal_not_a_missing_one():
    """0.0 hours meant both "no cycle" and "closed instantly".

    A cycle whose legs share a timestamp is the most suspicious shape this rail
    can see, and the sentinel silently excluded exactly that one - while a later
    slower cycle could overwrite it and be reported as the fastest.
    """
    graph = TransferGraph()
    graph.add("A", "B", 100_000, 1000.0)
    graph.add("B", "A", 100_000, 1000.0)

    structure = graph.structure("A", 1000.0)
    assert structure.cycle_count == 1
    assert structure.fastest_cycle_hours == 0.0

    names = {s.name for s in WireScorer()._signals(structure, "sender")}
    assert "tight_cycle" in names, "an instantaneous cycle must fire the signal"


def test_the_graph_forgets_accounts_as_well_as_edges():
    """Zeroed totals were left behind, so memory grew with account churn."""
    graph = TransferGraph()
    for i in range(50):
        graph.add(f"a{i}", f"b{i}", 1_000, i * 1e9)

    graph.add("fresh", "other", 1_000, 40 * NANOS_PER_DAY)

    assert len(graph._totals) <= 2, (
        f"{len(graph._totals)} zeroed totals entries survived eviction")


def test_velocity_counters_do_not_saturate():
    """A capped deque made a day and a week indistinguishable.

    At 512 stamps an account transacting once a minute pinned txns_last_day and
    txns_last_week to the same number after eight hours - on exactly the
    high-throughput accounts these features exist to separate.
    """
    history = AccountHistory()
    for i in range(700):
        observation = history.observe(
            context("acct", "shop", 10_000, i * 60e9, TransactionType.PAYMENT))

    assert observation.txns_last_day > 512, (
        f"txns_last_day saturated at {observation.txns_last_day}")
    assert observation.txns_last_hour < observation.txns_last_day, (
        "an hour and a day must not report the same count")


def test_eviction_is_not_skipped_by_unscored_traffic():
    """The sweep ran on a modulus of a counter unscored types also advance.

    Unscored types return before the sweep is reached, so every time the
    boundary landed on a cash-in the history went another full interval
    unpruned.
    """
    from risk.engine import EVICT_EVERY

    engine = FraudRiskEngine()
    for i in range(EVICT_EVERY - 1):
        engine.assess(context("a", "b", 100, i * 1e9, TransactionType.CASH_IN))
    assert engine._since_evict == 0, "unscored traffic advanced the sweep counter"


def test_a_failing_rail_is_counted_not_absorbed():
    """A rail failing every call and one finding nothing must look different."""
    class Exploding:
        history = None

        def assess(self, context):
            raise RuntimeError("model file is corrupt")

    engine = FraudRiskEngine(card_scorer=Exploding())
    decision = engine.assess(context("a", "b", 100, 0.0, TransactionType.PAYMENT))

    assert decision.action is RiskAction.ALLOW, "a broken rail must not block"
    assert engine.summary()["failed"] == 1, "the failure was absorbed silently"


def test_cases_are_bounded():
    """Every other store here is bounded; this one grew for the run's lifetime."""
    from sim.core.interfaces import RiskDecision

    from risk.engine import MAX_CASES

    engine = FraudRiskEngine()
    flagged = RiskDecision(action=RiskAction.REVIEW, score=0.9, rail="wire",
                           reason="fixture")
    for _ in range(MAX_CASES + 500):
        engine._record(context("a", "b", 100, 0.0), flagged)

    assert len(engine.cases) == MAX_CASES


def test_two_histories_are_refused_rather_than_silently_split():
    """A scorer with its own history detaches every count from what is scored."""
    from risk.card.scorer import UntrainedCardScorer

    with pytest.raises(ValueError, match="different AccountHistory"):
        FraudRiskEngine(card_scorer=UntrainedCardScorer(history=AccountHistory()),
                        history=AccountHistory())


def test_a_checkpoint_round_trip_keeps_its_shape(tmp_path):
    """save() writes self.config, and load() had been dropping the stored one.

    A resumed fine-tune or a re-export then stamped the default shape onto
    non-default weights, and the next load built a default-sized model for a
    mismatched state dict - the silent size mismatch the module exists to
    prevent.
    """
    from risk.card.encoding import Vocabulary, build_schema
    from risk.card.model import TorchSequenceModel
    from risk.card.treasure.config import ModelConfig
    from risk.card.treasure.model import build_model

    config = {"d_model": 32, "n_layers": 2, "n_heads": 2, "input_layers": 1,
              "max_seq_len": 32}
    vocab = Vocabulary(tables={"tx_type": {"PAYMENT": 1}, "gateway_id": {"g": 1},
                               "device_type": {"MOBILE": 1},
                               "hour_of_day": {"1": 1}, "day_of_week": {"1": 1},
                               "account_type": {"PERSONAL": 1},
                               "kyc_level": {"1": 1}})
    model = build_model(build_schema(vocab), ModelConfig(**config))

    first = tmp_path / "a.pt"
    TorchSequenceModel(model, vocab, config=config).save(first)

    second = tmp_path / "b.pt"
    TorchSequenceModel.load(first).save(second)

    assert TorchSequenceModel.load(second).config["d_model"] == 32, (
        "the stored shape was replaced by the default on a round trip")


# --- gaps a red-team review exposed -----------------------------------------

def test_volume_is_counted_separately_from_distinct_partners():
    """Structuring splits one sum across many sends, not many partners.

    Fifteen rapid transfers across two accounts scores 2 on fan-out and slips
    under any sensible distinct-payee threshold. Counting sends catches it.
    """
    graph = TransferGraph()
    for i in range(15):
        graph.add("mule", f"dest{i % 2}", 95_000, i * 60e9)

    structure = graph.structure("mule", 15 * 60e9)
    assert structure.fan_out_burst == 2, "only two distinct destinations"
    assert structure.sent_burst == 15, "but fifteen transfers"

    names = {s.name for s in WireScorer()._signals(structure, "sender")}
    assert "send_burst" in names, "a fifteen-transfer burst must raise something"


def test_a_card_test_can_be_challenged():
    """Elkan's rule is right per transaction and wrong for card testing.

    At a single 0.99 ceiling a 25-rupee payment needed 0.952 confidence to be
    challenged - and card testing uses exactly those amounts, on purpose,
    because a 25-rupee loss is not worth stopping. The cost of missing it is
    not 25 rupees; it is the large fraud the validated card then funds.
    """
    bands = AmountAwareBands()
    tiny = 2_500          # 25 rupees

    assert bands.decide(0.80, tiny) is RiskAction.STEP_UP, (
        "a confident model must be able to challenge a card test")
    assert bands.decide(0.40, tiny) is RiskAction.ALLOW, (
        "an unconfident score on a tiny payment must still pass")


def test_blocking_a_tiny_payment_still_needs_near_certainty():
    """A step-up is recoverable in ten seconds; a decline is a lost sale."""
    bands = AmountAwareBands()
    assert bands.block_threshold(100) >= 0.95
    for amount in (1, 2_500, 100_000, 10_000_000):
        assert bands.block_threshold(amount) >= bands.step_up_threshold(amount)


def test_a_large_payment_still_faces_a_lower_bar_than_a_small_one():
    """Splitting the ceilings must not flatten the curve it sits on."""
    bands = AmountAwareBands()
    assert bands.step_up_threshold(50_000_000) < bands.step_up_threshold(100_000)
    assert bands.step_up_threshold(100_000) < bands.step_up_threshold(2_500)


# --- facts the graph structurally cannot see ---------------------------------

def context_with(**overrides):
    """A transfer context with the fields these two signals read."""
    from sim.core.interfaces import RiskContext

    base = dict(
        tx_id="tx", tx_type=TransactionType.TRANSFER, actor_id="owner-a",
        source_account_id="a", destination_account_id="b", amount_paise=50_000,
        sim_time_ns=0.0, gateway_id="gw", device_type=DeviceType.MOBILE,
        source_account_type=AccountType.PERSONAL, source_kyc_level=1)
    base.update(overrides)
    return RiskContext(**base)


def test_paying_into_a_dead_account_raises_a_case():
    """A frozen account is under review; a closed one should receive nothing.

    This is a fact rather than an inference, so unlike the structural signals it
    is weighted to raise a case on its own.
    """
    from sim.core.interfaces import AccountStatus

    for status in (AccountStatus.FROZEN, AccountStatus.CLOSED,
                   AccountStatus.DISPUTED):
        decision = WireScorer().assess(context_with(destination_status=status))
        assert decision.action is RiskAction.REVIEW, (
            f"a transfer into a {status.value} account passed unflagged")
        assert status.value.lower() in decision.reason.lower()

    live = WireScorer().assess(context_with(destination_status=AccountStatus.ACTIVE))
    assert live.action is RiskAction.ALLOW, "an active destination is ordinary"


def test_moving_money_between_your_own_accounts_is_a_weak_signal():
    """Self-dealing is what anyone with a savings account does.

    It is only meaningful in combination, so it must not raise a case alone -
    otherwise every transfer to your own savings becomes a laundering alert.
    """
    scorer = WireScorer()
    same_owner = scorer.assess(context_with(source_owner_id="alice",
                                            destination_owner_id="alice"))
    assert same_owner.action is RiskAction.ALLOW, (
        "a self-transfer on its own must not raise a case")
    assert same_owner.score > 0, "but it should contribute to the score"

    different = WireScorer().assess(context_with(source_owner_id="alice",
                                                 destination_owner_id="bob"))
    assert different.score < same_owner.score


def test_two_unknown_owners_are_not_a_match():
    """Empty means the engine did not supply an owner, not that they are equal."""
    decision = WireScorer().assess(context_with(source_owner_id="",
                                                destination_owner_id=""))
    assert decision.score == 0.0, "two unknowns were treated as the same person"


def test_the_engine_supplies_the_new_fields():
    """The signals are useless if the context arrives with them empty."""
    from sim.core.events import AccountCreated
    from sim.core.interfaces import AccountStatus, Command

    seen = []

    class Recording:
        def assess(self, context):
            seen.append(context)
            from sim.core.interfaces import RiskDecision
            return RiskDecision.allow(rail="wire")

    engine = world_engine(risk=Recording())
    engine.execute_command(Command(
        command_id="c", actor_id="user1", action_type=TransactionType.TRANSFER,
        source_account_id="acc1", target_account_id="acc2", amount_paise=500,
        idempotency_key="k"))

    assert seen, "the rail was never consulted"
    context = seen[-1]
    assert context.source_owner_id == "user1"
    assert context.destination_owner_id == "user2"
    assert context.destination_status is AccountStatus.ACTIVE


# --- the red-team playbook, pattern by pattern -------------------------------
#
# `agents/redteam/personas.py` tells the attacker exactly which eight gaps to
# aim at. Each test below drives one of them and asserts the rails notice.
# These are the patterns the system will actually be attacked with, so "a
# detector exists" is not enough - it has to fire on the shape as described.

def transfer(source, destination, amount, at_ns, owner=None, dest_owner=None,
             tx_type=TransactionType.TRANSFER):
    from sim.core.interfaces import RiskContext

    return RiskContext(
        tx_id=f"tx-{at_ns}", tx_type=tx_type, actor_id=owner or f"owner-{source}",
        source_account_id=source, destination_account_id=destination,
        amount_paise=amount, sim_time_ns=at_ns, gateway_id="gw",
        device_type=DeviceType.MOBILE, source_account_type=AccountType.PERSONAL,
        source_kyc_level=1, source_owner_id=owner or f"owner-{source}",
        destination_owner_id=dest_owner or f"owner-{destination}")


def test_playbook_1_unbounded_inbound_value():
    """"Drive far more value into an account than its tier could ever send."

    `fan_in` counts *payers*, so value arriving from two accounts registers on
    nothing. This is the playbook's opening gap and the mule primitive.
    """
    scorer = WireScorer()
    decision = None
    for i in range(4):
        decision = scorer.assess(transfer(f"payer{i % 2}", "mule",
                                          800_000, i * 60e9))

    structure = scorer.graph.structure("mule", 4 * 60e9)
    assert structure.fan_in_burst == 2, "only two distinct payers"
    assert structure.received_burst == 3_200_000, "but a large sum arrived"
    assert decision.action is RiskAction.REVIEW


def test_playbook_3_one_owner_many_accounts():
    """"A total, moved by you, that exceeds any single account's allowance."

    Every transfer is individually legal and from a different account. Only
    summing per owner shows it.
    """
    scorer = WireScorer()
    decision = None
    for i in range(6):
        # Six accounts, one owner, each moving a modest amount to a stranger.
        decision = scorer.assess(transfer(f"acct{i}", f"other{i}", 1_000_000,
                                          i * 600e9, owner="attacker"))

    value, count = scorer.owner_volume("attacker", 6 * 600e9)
    assert value == 6_000_000 and count == 6
    assert decision.action is RiskAction.REVIEW
    assert "owner moved" in decision.reason


def test_playbook_7_cash_out_is_seen():
    """"Value reconverging into one account and leaving."

    The exit leg was not scored at all, so the rails watched money pool up and
    then lost sight of it at the moment the pattern completes.
    """
    from risk.engine import WIRE_TYPES

    assert TransactionType.CASH_OUT in WIRE_TYPES, (
        "cash-out is where laundering ends; a rail that cannot see it watches "
        "the money arrive and never leave")

    engine = FraudRiskEngine()
    decision = engine.assess(transfer("mule", "CASH_ENTITY", 500_000, 0.0,
                                      tx_type=TransactionType.CASH_OUT))
    assert decision.rail == "wire", "cash-out reached no rail"
    assert engine.summary()["scored"] == 1


def test_playbook_4_and_5_rolling_windows_and_counts():
    """"Counters reset on the sim-day boundary" and "count is never enforced".

    Every window here rolls, so there is no boundary to wait for, and
    `send_burst` counts transactions where the engine only caps value.
    """
    scorer = WireScorer()
    decision = None
    # Fifteen sends, each individually small, across a single day boundary.
    for i in range(15):
        decision = scorer.assess(transfer("attacker", f"dest{i % 3}", 90_000,
                                          i * 1200e9))

    assert decision.action is RiskAction.REVIEW, (
        "a fifteen-transfer burst crossing a day boundary must still register")


def test_the_red_team_runs_defended_by_default():
    """An attack against a system with the controls off measures nothing.

    Risk is off by default everywhere else, which keeps the engine
    byte-identical for replay tests. The red-team entry point is the one place
    that default is wrong.
    """
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[2] / "scripts" / "red_team_run.py"
    text = source.read_text(encoding="utf-8")
    assert "enable_risk=not args.no_risk" in text, (
        "the red-team runner must enable the rails unless asked not to")
    assert "--no-risk" in text, "there must be a way to run undefended on purpose"
