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

from dataclasses import dataclass

from risk.wire.graph import AccountStructure, TransferGraph
from sim.core.interfaces import RiskAction, RiskContext, RiskDecision

RAIL = "wire"


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

    # A mule forwards nearly everything it receives. Ordinary accounts keep a
    # balance, so the ratio sits well below one.
    passthrough_ratio: float = 0.90
    passthrough_min_received_paise: int = 100_000       # 1,000 rupees

    # Fan-out inside six hours to many counterparties. A payroll run does this
    # too, which is why it is a signal and not a verdict.
    fan_out_burst: int = 8
    fan_in_burst: int = 8

    # A loop that closes inside a day, through few enough hops to be deliberate.
    cycle_hours: float = 24.0
    cycle_max_length: int = 4

    # The score at which a case is raised. Set so that no single signal is
    # enough on its own - laundering is a combination of structure and speed,
    # and any one of these alone has an innocent explanation.
    review_at: float = 0.60


class WireScorer:
    """Watches every transfer and raises cases on structure. Blocks nothing."""

    def __init__(self, graph: TransferGraph | None = None,
                 thresholds: WireThresholds | None = None) -> None:
        # Same trap as AccountHistory: TransferGraph defines __len__, so an
        # empty graph is falsy and `or` would quietly replace it.
        self.graph = TransferGraph() if graph is None else graph
        self.thresholds = thresholds or WireThresholds()

    def observe(self, context: RiskContext) -> None:
        """Fold a transfer into the graph without scoring it."""
        self.graph.add(context.source_account_id, context.destination_account_id,
                       context.amount_paise, context.sim_time_ns)

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

        signals = self._signals(source, "sender") + self._signals(destination, "recipient")
        score = min(1.0, sum(s.weight for s in signals))

        if score >= self.thresholds.review_at:
            return RiskDecision(
                action=RiskAction.REVIEW, score=score, rail=RAIL,
                reason="; ".join(s.detail for s in signals),
            )
        return RiskDecision.allow(rail=RAIL, score=score)

    def _signals(self, structure: AccountStructure, side: str) -> list[Signal]:
        """Which patterns this account matches, and how much each is worth."""
        limits = self.thresholds
        signals: list[Signal] = []

        if (structure.received_total >= limits.passthrough_min_received_paise
                and structure.passthrough >= limits.passthrough_ratio):
            signals.append(Signal(
                "passthrough", 0.35,
                f"{side} forwarded {structure.passthrough:.0%} of what it received"))

        if structure.fan_out_burst >= limits.fan_out_burst:
            signals.append(Signal(
                "fan_out", 0.30,
                f"{side} paid {structure.fan_out_burst} accounts within six hours"))

        if structure.fan_in_burst >= limits.fan_in_burst:
            signals.append(Signal(
                "fan_in", 0.30,
                f"{side} was paid by {structure.fan_in_burst} accounts within six hours"))

        if (structure.cycle_count
                and 0 < structure.shortest_cycle <= limits.cycle_max_length
                and 0 < structure.fastest_cycle_hours <= limits.cycle_hours):
            signals.append(Signal(
                "tight_cycle", 0.40,
                f"{side} sits on {structure.cycle_count} cycle(s), shortest "
                f"{structure.shortest_cycle} hops, fastest closing in "
                f"{structure.fastest_cycle_hours:.1f}h"))

        return signals
