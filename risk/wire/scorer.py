"""Deciding when an account's structure is worth a human's time.

**This rail never stops a transfer.** Laundering detection runs at roughly 12%
precision, so blocking automatically would refuse about eight innocent transfers
for every real laundering leg. It raises a case; a named reviewer decides
whether to freeze, and the freeze is a separate action with its own record.

There is a second reason, and it is not a modelling one. Telling a customer they
are under money-laundering review is **tipping off** - a criminal offence under
India's PMLA, the US Bank Secrecy Act and the UK's Proceeds of Crime Act. A
system that declined a transfer with "flagged as suspicious" would commit that
offence automatically, several times a day.

## What this is, and what it is not

These are **structural facts**, not a model's probability. "This account sits on
two cycles that closed within a day and forwarded 96% of what it received" is
something the graph either shows or does not. Nothing here is calibrated against
observed laundering rates, because no laundering model has been trained on this
simulator's traffic yet.

That distinction is the whole reason the score below is built from named,
readable conditions rather than fitted weights: every case it raises can be
explained to the reviewer who has to act on it, and none of it claims a
precision it has not measured.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, replace

from risk.wire.graph import AccountStructure, TransferGraph
from sim.core.interfaces import (
    AccountStatus,
    RiskAction,
    RiskContext,
    RiskDecision,
)

log = logging.getLogger(__name__)

RAIL = "wire"

# One day. The per-account cap the engine enforces is daily, so the aggregate
# that defeats it is measured over the same period.
OWNER_WINDOW_NS = 24 * 3_600 * 1_000_000_000


# Two kinds of signal, weighted differently on purpose.
#
# A **structural inference** - a shape in the graph that laundering tends to
# produce and ordinary business sometimes also produces. Fan-out looks like a
# payroll run; a cycle looks like a supply chain. None of these is worth a
# reviewer's time alone, so each carries about a third of the bar and a case
# needs two of them.
#
# A **quantitative fact** - a stated limit, breached, with the number attached.
# "This account was credited 32,000 rupees in six hours" is not an inference
# about intent; it either happened or it did not. The red-team playbook is
# explicit that these are the findings ("always carry a NUMBER: a total, a
# ratio, a rate"), and a rail that needs two of them before saying anything
# would miss the single-primitive attacks it is built to catch.
INFERENCE = 0.30
FACT = 0.60


@dataclass(frozen=True)
class Signal:
    """One structural observation, with the evidence that produced it."""

    name: str
    weight: float
    detail: str


@dataclass(frozen=True)
class WireThresholds:
    """Where each structural pattern starts being worth a look.

    Every value here is a judgement, not a measurement, and is written down so
    it can be argued with and later replaced by something fitted to observed
    traffic.
    """

    # A mule forwards nearly everything it receives - so the ratio sits just
    # *below* one, and a band is needed rather than a floor.
    #
    # A floor alone was wrong and measurably so: `passthrough` is unbounded, and
    # an ordinary account that spends money arriving from outside the transfer
    # graph - a salary, a deposit - reaches 2.46. Reading "at least 0.90"
    # flagged 80% of legitimate traffic. Sending far more than you received is
    # not the pass-through pattern; it is the opposite of it.
    passthrough_ratio: float = 0.85
    passthrough_ceiling: float = 1.15
    passthrough_min_received_paise: int = 100_000       # 1,000 rupees

    # Fan-out inside six hours to many counterparties. A payroll run does this
    # too, which is why it is a signal and not a verdict.
    fan_out_burst: int = 8
    fan_in_burst: int = 8

    # Sheer volume of sends inside the same window, repeats included. Distinct
    # counterparties and volume are different shapes, and structuring produces
    # the second without the first: fifteen transfers split across two accounts
    # scores 2 on fan-out and slips under it.
    sent_burst: int = 12

    # A loop that closes inside a day, through few enough hops to be deliberate.
    cycle_hours: float = 24.0
    cycle_max_length: int = 4

    # What one owner may move across all of its accounts inside a day before
    # it is worth a look. Set well above an ordinary person's daily activity -
    # this is meant to catch an actor operating a fleet of accounts, not
    # somebody paying rent.
    owner_volume_paise: int = 5_000_000        # 50,000 rupees

    # Value arriving at one account inside the tight window. `fan_in` counts
    # *payers*; this counts money. An account credited far beyond what its own
    # tier would let it send is the mule-account primitive, and counting
    # distinct senders misses it whenever the value arrives from few sources.
    received_burst_paise: int = 2_000_000      # 20,000 rupees

    # Paying into an account that is not live. A frozen or disputed account is
    # under review already, and a closed one should not be receiving money at
    # all - so a transfer into either is worth a look regardless of shape.
    # Weighted to raise a case on its own, because unlike the structural
    # signals this is a fact rather than an inference.
    dead_destination: float = 0.65

    # Moving money between accounts you own is not laundering by itself - it is
    # what anyone with a savings account does. It becomes a signal only in
    # combination, which is why it is weighted low: a self-transfer that also
    # sits on a tight cycle or forwards everything it receives is layering
    # through accounts a single person controls.
    self_dealing: float = 0.25

    # Where the IBM-trained model's score starts contributing, and how much.
    #
    # An INFERENCE weight on purpose. The model runs here with four of its
    # features constant - together 31.6% of its training gain, including its
    # strongest - so it is evidence rather than a verdict, and a case still
    # needs something structural alongside it. That conjunction is the whole
    # reason this can help where blending the two rankings measurably could not.
    #
    # The threshold is **fitted**, and the default is deliberately unreachable.
    # Those same constants compress the model's output: on simulator traffic it
    # never exceeds 0.27, so any hand-picked bar near 0.5 means the signal
    # silently never fires - which is what a first attempt did, producing two
    # arms with byte-identical results and the appearance of "no effect". A
    # probability from a model whose best splits are inert is not comparable to
    # one from the model as trained, so the bar has to come from this traffic.
    model_score: float = 1.0

    # The score at which a case is raised.
    #
    # This default is a guess and should be replaced by `calibrate` below. A
    # hand-picked bar is the wrong tool: it was originally set to 0.60 on the
    # reasoning that no single signal should suffice, and a textbook mule chain
    # - money in from six accounts, 93% of it straight back out to six others
    # within the hour - scored 0.35 and passed unflagged.
    #
    # The lesson is not "lower it until that case passes", which is fitting the
    # bar to one example. It is that a threshold has to be set against the
    # distribution of real traffic, so that flagging a fixed share of it means
    # something operationally.
    review_at: float = 0.60


def calibrate(transfers, target_flag_rate: float = 0.02,
              thresholds: WireThresholds | None = None,
              model=None) -> WireThresholds:
    """Fit the thresholds to observed traffic instead of guessing them.

    Review capacity is the real constraint - a team can look at so many cases a
    day and no more - so the useful question is "what bar spends exactly that
    budget", not "what number feels suspicious".

    **The value thresholds are fitted too, and have to be.** A limit written in
    rupees is a guess about a currency and an economy. Measured on simulator
    traffic, the hand-set 50,000-rupee owner limit sat *below* what an ordinary
    account moves in a day and *above* what a mule chain moves in six legs - so
    it flagged the honest population and missed every attacker. A percentile of
    what this population actually does cannot make that mistake: it is defined
    relative to the traffic rather than to an amount someone imagined.

    Pass traffic **without** known fraud in it. Calibrating on a mixture sets
    the bar above some of the fraud, which is the opposite of what it is for.
    """
    thresholds = thresholds or WireThresholds()

    # The value limits are fitted first, then the bar is fitted against the
    # scores they produce. Order matters: fitting the bar against scores from
    # guessed limits sets it for a distribution that will not exist once the
    # limits move.
    #
    # The percentile is derived from the flag-rate budget rather than fixed. A
    # value signal weighs as a fact, so any transfer crossing a value limit
    # raises a case on its own - which means the share of traffic above the
    # limit *is* roughly the flag rate. A fixed 0.995 measured 14% flagged,
    # because in this traffic many accounts legitimately cross a 99.5th
    # percentile inside a six-hour window.
    value_percentile = 1.0 - target_flag_rate / 2
    thresholds = _fit_value_thresholds(transfers, thresholds, value_percentile)
    if model is not None:
        thresholds = _fit_model_threshold(transfers, thresholds, model,
                                          value_percentile)

    scorer = WireScorer(thresholds=thresholds, model=model)
    scores = sorted((scorer.assess(t).score for t in transfers), reverse=True)

    if not scores:
        raise ValueError("no transfers to calibrate on")

    index = min(int(len(scores) * target_flag_rate), len(scores) - 1)
    bar = scores[index]

    # Scores are discrete - they are sums of a handful of fixed weights - so the
    # percentile often lands on a value shared by many transfers, and a bar set
    # exactly there flags all of them. Nudging above it keeps the flag rate at
    # or under the budget rather than overshooting it.
    above = [s for s in scores if s > bar]
    if above and len(above) / len(scores) <= target_flag_rate:
        bar = min(above)

    return replace(thresholds, review_at=bar)


def _fit_value_thresholds(transfers, thresholds: WireThresholds,
                          percentile: float) -> WireThresholds:
    """Set the value limits above what this population ordinarily does.

    The percentile comes from the flag-rate budget, because a value signal
    weighs as a fact and raises a case on its own - so the share of traffic
    above a limit is roughly the share flagged by it. Half the budget is
    allocated to the value signals, leaving the rest for the structural ones.

    Fitted on clean traffic, "ordinary" is exactly what the sample contains.
    """
    observer = WireScorer(thresholds=thresholds)
    received: list[int] = []
    owner_moved: list[int] = []

    for context in transfers:
        observer.observe(context)
        structure = observer.graph.structure(context.destination_account_id,
                                             context.sim_time_ns)
        received.append(structure.received_burst)
        if context.source_owner_id:
            value, _ = observer.owner_volume(context.source_owner_id,
                                             context.sim_time_ns)
            owner_moved.append(value)

    def at(values: list[int], fallback: int) -> int:
        if not values:
            return fallback
        ordered = sorted(values)
        index = min(int(len(ordered) * percentile), len(ordered) - 1)
        # Strictly above the percentile, so the observed value that defined it
        # is itself not flagged.
        return max(ordered[index] + 1, 1)

    return replace(
        thresholds,
        received_burst_paise=at(received, thresholds.received_burst_paise),
        owner_volume_paise=at(owner_moved, thresholds.owner_volume_paise),
    )


def _fit_model_threshold(transfers, thresholds: WireThresholds, model,
                         percentile: float) -> WireThresholds:
    """Set the model's bar at a percentile of what it says about clean traffic.

    A probability means something only relative to the distribution the model
    produces, and this one's distribution is compressed by its constant
    features. Fitted here, "unusual for this model on this traffic" is what the
    signal actually tests.
    """
    observer = WireScorer(thresholds=thresholds)
    scores = []
    for context in transfers:
        observer.observe(context)
        source = observer.graph.structure(context.source_account_id,
                                          context.sim_time_ns)
        destination = observer.graph.structure(context.destination_account_id,
                                               context.sim_time_ns)
        try:
            scores.append(model.score(context, source, destination))
        except Exception:
            log.exception("wire model raised while calibrating")
            return thresholds

    if not scores:
        return thresholds

    ordered = sorted(scores)
    index = min(int(len(ordered) * percentile), len(ordered) - 1)
    return replace(thresholds, model_score=ordered[index])


class WireScorer:
    """Watches every transfer and raises cases on structure. Blocks nothing."""

    def __init__(self, graph: TransferGraph | None = None,
                 thresholds: WireThresholds | None = None,
                 model=None) -> None:
        # Same trap as AccountHistory: TransferGraph defines __len__, so an
        # empty graph is falsy and `or` would quietly replace it.
        self.graph = TransferGraph() if graph is None else graph
        self.thresholds = thresholds or WireThresholds()
        # Optional. The rules score better alone than any blend measured, so a
        # missing model is a configuration rather than a failure.
        self.model = model
        # Value moved per owner inside the rolling window, and per account.
        # The engine caps a single account's daily volume and nothing sums what
        # one actor moved across every account it controls - so an attacker
        # provisions several accounts, keeps each individually legal, and moves
        # a total no single cap would ever have allowed. The graph cannot see
        # this: ownership is not an edge.
        self._owner_sent: dict[str, list[tuple[float, int]]] = defaultdict(list)

    def observe(self, context: RiskContext) -> None:
        """Fold a transfer into the graph without scoring it."""
        self.graph.add(context.source_account_id, context.destination_account_id,
                       context.amount_paise, context.sim_time_ns)
        if context.source_owner_id:
            self._record_owner_volume(context)

    def _record_owner_volume(self, context: RiskContext) -> None:
        """Track what one owner moved, across every account it controls."""
        moves = self._owner_sent[context.source_owner_id]
        moves.append((context.sim_time_ns, context.amount_paise))
        # Trimmed to the same window the graph keeps, so this cannot grow
        # without bound any more than the graph does.
        cutoff = context.sim_time_ns - OWNER_WINDOW_NS
        while moves and moves[0][0] < cutoff:
            moves.pop(0)
        if not moves:
            del self._owner_sent[context.source_owner_id]

    def owner_volume(self, owner_id: str, now_ns: float) -> tuple[int, int]:
        """(value, count) this owner moved inside the window."""
        cutoff = now_ns - OWNER_WINDOW_NS
        moves = [m for m in self._owner_sent.get(owner_id, []) if m[0] >= cutoff]
        return sum(m[1] for m in moves), len(moves)

    def assess(self, context: RiskContext) -> RiskDecision:
        """Record the transfer, then judge the accounts on both ends.

        The transfer is added to the graph *before* the structure is read, so a
        laundering leg is scored against the graph it just created rather than
        the one that existed a moment earlier. A pattern that completes on this
        transfer is exactly the case worth catching.
        """
        self.observe(context)

        source = self.graph.structure(context.source_account_id, context.sim_time_ns)
        destination = self.graph.structure(context.destination_account_id,
                                           context.sim_time_ns)

        signals = (self._signals(source, "sender")
                   + self._signals(destination, "recipient")
                   + self._context_signals(context)
                   + self._model_signal(context, source, destination))
        score = min(1.0, sum(s.weight for s in signals))

        if score >= self.thresholds.review_at:
            return RiskDecision(
                action=RiskAction.REVIEW, score=score, rail=RAIL,
                reason="; ".join(s.detail for s in signals),
            )
        return RiskDecision.allow(rail=RAIL, score=score)

    def _model_signal(self, context: RiskContext, source, destination) -> list[Signal]:
        """What the trained model makes of this transfer, if one is wired.

        A failure here is swallowed. The rules are the rail; the model is one
        more opinion, and a rail that stopped working because an optional
        booster raised would be worse than one running without it.
        """
        if self.model is None:
            return []
        try:
            score = self.model.score(context, source, destination)
        except Exception:
            log.exception("wire model raised; scoring on the rules alone")
            return []

        if score < self.thresholds.model_score:
            return []
        return [Signal("model", INFERENCE,
                       f"the trained model scores this {score:.2f}")]

    def _context_signals(self, context: RiskContext) -> list[Signal]:
        """Facts about the transaction itself, rather than the graph shape.

        These are things the account graph structurally cannot see. Who owns an
        account is not an edge, and an account's status is not a shape - both
        arrive on the context and were being thrown away.
        """
        signals: list[Signal] = []
        limits = self.thresholds

        # What this owner has moved across every account it controls. The
        # per-account daily cap says nothing about this total, which is exactly
        # the gap: every individual transfer is legal and the sum is not.
        if context.source_owner_id:
            value, count = self.owner_volume(context.source_owner_id,
                                             context.sim_time_ns)
            if value >= limits.owner_volume_paise:
                signals.append(Signal(
                    "owner_volume", FACT,
                    f"sender's owner moved {value / 100:,.0f} across "
                    f"{count} transfers in a day"))

        status = context.destination_status
        if status is not None and status is not AccountStatus.ACTIVE:
            signals.append(Signal(
                "dead_destination", limits.dead_destination,
                f"destination account is {status.value}"))

        # Both ends known and the same person on each. Empty strings mean the
        # engine did not supply owners, and two unknowns are not a match.
        source_owner = context.source_owner_id
        destination_owner = context.destination_owner_id
        if source_owner and destination_owner and source_owner == destination_owner:
            signals.append(Signal(
                "self_dealing", limits.self_dealing,
                "both accounts have the same owner"))

        return signals

    def _signals(self, structure: AccountStructure, side: str) -> list[Signal]:
        """Which patterns this account matches, and how much each is worth."""
        limits = self.thresholds
        signals: list[Signal] = []

        if (structure.received_total >= limits.passthrough_min_received_paise
                and limits.passthrough_ratio <= structure.passthrough
                <= limits.passthrough_ceiling):
            signals.append(Signal(
                "passthrough", 0.35,
                f"{side} forwarded {structure.passthrough:.0%} of what it received"))

        if structure.fan_out_burst >= limits.fan_out_burst:
            signals.append(Signal(
                "fan_out", INFERENCE,
                f"{side} paid {structure.fan_out_burst} accounts within six hours"))

        if structure.fan_in_burst >= limits.fan_in_burst:
            signals.append(Signal(
                "fan_in", INFERENCE,
                f"{side} was paid by {structure.fan_in_burst} accounts within six hours"))

        if structure.received_burst >= limits.received_burst_paise:
            signals.append(Signal(
                "received_burst", FACT,
                f"{side} was credited {structure.received_burst / 100:,.0f} "
                f"within six hours"))

        if structure.sent_burst >= limits.sent_burst:
            signals.append(Signal(
                "send_burst", FACT,
                f"{side} made {structure.sent_burst} transfers within six hours"))

        # Gated on the cycle existing, not on its speed being above zero. A
        # cycle closing in 0.0 hours is instantaneous, which is the strongest
        # signal available - and `0 < fastest` silently excluded exactly that.
        if (structure.cycle_count
                and 0 < structure.shortest_cycle <= limits.cycle_max_length
                and structure.fastest_cycle_hours <= limits.cycle_hours):
            signals.append(Signal(
                "tight_cycle", 0.40,
                f"{side} sits on {structure.cycle_count} cycle(s), shortest "
                f"{structure.shortest_cycle} hops, fastest closing in "
                f"{structure.fastest_cycle_hours:.1f}h"))

        return signals
