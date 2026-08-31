"""The one object the simulation is given: routes a transaction to its rail.

Two rails, and which one applies is decided by the transaction type, not by the
model. That separation is the point:

  **PAYMENT** goes to the card rail, which may block or challenge. Someone
      spending money that is not theirs causes an immediate, bounded loss, and a
      wrong decision is cheap to recover from - a genuine customer confirms with
      a code and carries on.

  **TRANSFER** goes to the wire rail, which never stops anything. Laundering
      detection runs at roughly 12% precision, and telling a customer they are
      under money-laundering review is a criminal offence in every jurisdiction
      this simulates. Flagged transfers become cases for a human.

  Everything else - cash in, cash out, fees, interest, settlement - is not
      scored. None of them is a route an attacker controls end to end, and
      scoring them would add false positives with no fraud behind them.

The class satisfies `sim.core.interfaces.RiskScorer`, which is the only thing
the engine knows about it.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from risk.card.history import AccountHistory
from risk.card.scorer import UntrainedCardScorer
from risk.collect import TrafficRecorder
from risk.wire.graph import TransferGraph
from risk.wire.scorer import WireScorer
from sim.core.interfaces import (
    RiskAction,
    RiskContext,
    RiskDecision,
    TransactionType,
)

log = logging.getLogger(__name__)

# Transaction types that reach a rail at all.
#
# CASH_OUT belongs on the wire rail because it is where laundering *ends*.
# Value pools in a mule account and leaves the system, and a rail that watched
# the money arrive but not depart lost sight of it at the one moment that
# completes the pattern - which is also the moment a reviewer most needs to see.
CARD_TYPES = frozenset({TransactionType.PAYMENT})
WIRE_TYPES = frozenset({TransactionType.TRANSFER, TransactionType.CASH_OUT})

# Stale accounts are swept out of the card history on a schedule rather than on
# every transaction, because a sweep over every account per payment would make
# scoring cost grow with the size of the simulation.
EVICT_EVERY = 10_000

# The console and the API read the most recent hundred or two. Older cases are
# in the decision log if a reviewer acted on them, so keeping every one forever
# retains memory nothing reads.
MAX_CASES = 5_000


@dataclass
class Case:
    """A flagged transaction, waiting for a human.

    Holds the evidence, not a conclusion. A reviewer reads `reason` and decides;
    nothing here freezes anything.
    """

    tx_id: str
    rail: str
    action: str
    score: float
    reason: str
    amount_paise: int
    source_account_id: str
    destination_account_id: str
    sim_time_ns: float
    # Where the money went after this leg. A case saying "this transfer looks
    # unusual" is not reviewable - the decision is whether to freeze an account,
    # and that needs the route: which accounts, how much, how fast, and where it
    # stopped. Empty on the card rail, which has no chain to follow.
    chain: list = field(default_factory=list)


@dataclass
class Counters:
    """What the rails have done. Read by the monitor and by tests."""

    assessed: int = 0
    allowed: int = 0
    stepped_up: int = 0
    blocked: int = 0
    review: int = 0
    unscored: int = 0
    failed: int = 0
    by_rail: dict[str, int] = field(default_factory=dict)


class FraudRiskEngine:
    """Both rails behind one `assess` call.

    Constructed with sensible defaults so the composition root can wire it in
    one line, and every part is replaceable so a trained model can be dropped in
    without touching the simulation.
    """

    def __init__(self, card_scorer=None, wire_scorer: WireScorer | None = None,
                 history: AccountHistory | None = None,
                 graph: TransferGraph | None = None,
                 recorder: TrafficRecorder | None = None) -> None:
        # `is None`, not `or`: AccountHistory defines __len__, so an empty
        # one is falsy and `history or AccountHistory()` would discard the
        # caller's object and silently build a second, separate history.
        self.history = AccountHistory() if history is None else history
        self.card = card_scorer or UntrainedCardScorer(history=self.history)
        # A scorer arriving with its own history detaches every count from what
        # is actually being scored: `_record_traffic` reads this object, the
        # summary reports its size, and the sweep prunes it - while the scorer
        # writes somewhere else. Nothing about that failure is visible, so it is
        # refused rather than trusted to the caller.
        scorer_history = getattr(self.card, "history", None)
        if scorer_history is not None and scorer_history is not self.history:
            raise ValueError(
                "the card scorer holds a different AccountHistory than the "
                "engine. Pass the same object to both, or pass only the "
                "scorer - two histories means the traffic recorder, the "
                "summary and the eviction sweep all read the wrong one.")
        self.wire = wire_scorer or WireScorer(
            graph=TransferGraph() if graph is None else graph)
        self.counters = Counters()
        # Bounded. The live API session runs indefinitely and reads only the
        # most recent hundred, so an unbounded list is retained memory that
        # nothing looks at - and every other store in this package is bounded.
        self.cases: deque[Case] = deque(maxlen=MAX_CASES)
        # Counted separately from `assessed`, which advances on unscored types
        # too - and those return before the sweep is reached, so a modulus on
        # `assessed` skips a whole 10,000 every time the boundary happens to
        # land on a cash-in or a fee. In a mix dominated by unscored types the
        # sweep effectively never fired.
        self._since_evict = 0
        # With no model trained yet, this is the point of running at all: the
        # history is built either way, and the recorder writes it down.
        self.recorder = recorder

    def assess(self, context: RiskContext) -> RiskDecision:
        """Score one transaction. Never raises.

        The simulation engine also treats an exception as ALLOW, but catching it
        here means a failure is *counted* rather than silently absorbed - a rail
        failing on every call and a rail finding nothing look identical from the
        outside otherwise. An earlier version documented that guarantee without
        implementing it, and left the counters unable to reconcile: `assessed`
        advanced while `_record` was skipped.
        """
        self.counters.assessed += 1

        try:
            if context.tx_type in CARD_TYPES:
                decision = self.card.assess(context)
                self._record_traffic(context)
            elif context.tx_type in WIRE_TYPES:
                decision = self.wire.assess(context)
            else:
                self.counters.unscored += 1
                return RiskDecision.allow(rail="none", reason="type is not scored")
        except Exception:
            self.counters.failed += 1
            log.exception("risk scorer raised; allowing the transaction")
            return RiskDecision.allow(rail="none", reason="risk scorer raised")

        self._record(context, decision)
        self._maybe_evict(context)
        return decision

    def _trace(self, context: RiskContext, decision: RiskDecision) -> list:
        """The route the money took, for a wire case.

        Only traced for cases that are actually raised. Tracing every transfer
        would walk the graph on the 98% of traffic nobody will ever look at.
        """
        if decision.rail != "wire":
            return []
        try:
            return self.wire.graph.trace_chain(context.source_account_id,
                                               context.sim_time_ns)
        except Exception:
            # A trace is context for a reviewer, not part of the decision. If it
            # fails the case is still worth raising without it.
            log.exception("chain trace failed")
            return []

    def _record_traffic(self, context: RiskContext) -> None:
        """Append this payment to the training set, if one is being collected.

        Read back out of the history rather than rebuilt, so the row written to
        disk is byte-identical to the one the scorer just used. Building it
        twice would be two implementations of the same mapping, which is exactly
        the drift `risk.card.encoding` exists to prevent.
        """
        if self.recorder is None:
            return
        sequence = self.history.sequence(context.source_account_id)
        if sequence:
            self.recorder.record(context, sequence[-1])

    def _record(self, context: RiskContext, decision: RiskDecision) -> None:
        counters = self.counters
        counters.by_rail[decision.rail] = counters.by_rail.get(decision.rail, 0) + 1

        if decision.action is RiskAction.ALLOW:
            counters.allowed += 1
            return

        if decision.action is RiskAction.STEP_UP:
            counters.stepped_up += 1
        elif decision.action is RiskAction.BLOCK:
            counters.blocked += 1
        elif decision.action is RiskAction.REVIEW:
            counters.review += 1

        self.cases.append(Case(
            tx_id=context.tx_id, rail=decision.rail,
            action=decision.action.value, score=decision.score,
            reason=decision.reason, amount_paise=context.amount_paise,
            source_account_id=context.source_account_id,
            destination_account_id=context.destination_account_id,
            sim_time_ns=context.sim_time_ns,
            chain=self._trace(context, decision),
        ))

    def _maybe_evict(self, context: RiskContext) -> None:
        self._since_evict += 1
        if self._since_evict >= EVICT_EVERY:
            self._since_evict = 0
            dropped = self.history.evict_stale(context.sim_time_ns)
            if dropped:
                log.debug("evicted %d stale accounts from card history", dropped)

    def summary(self) -> dict:
        """A flat view for the monitor, a report, or an assertion in a test."""
        counters = self.counters
        flagged = counters.stepped_up + counters.blocked + counters.review
        return {
            "assessed": counters.assessed,
            "scored": counters.assessed - counters.unscored,
            "allowed": counters.allowed,
            "stepped_up": counters.stepped_up,
            "blocked": counters.blocked,
            "review": counters.review,
            "flagged": flagged,
            "failed": counters.failed,
            "flag_rate": flagged / max(counters.assessed - counters.unscored, 1),
            "by_rail": dict(counters.by_rail),
            "open_cases": len(self.cases),
            "accounts_tracked": len(self.history),
        }
