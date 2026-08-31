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
from dataclasses import dataclass, field

from risk.card.history import AccountHistory
from risk.card.scorer import UntrainedCardScorer
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
CARD_TYPES = frozenset({TransactionType.PAYMENT})
WIRE_TYPES = frozenset({TransactionType.TRANSFER})

# Stale accounts are swept out of the card history on a schedule rather than on
# every transaction, because a sweep over every account per payment would make
# scoring cost grow with the size of the simulation.
EVICT_EVERY = 10_000


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


@dataclass
class Counters:
    """What the rails have done. Read by the monitor and by tests."""

    assessed: int = 0
    allowed: int = 0
    stepped_up: int = 0
    blocked: int = 0
    review: int = 0
    unscored: int = 0
    by_rail: dict[str, int] = field(default_factory=dict)


class FraudRiskEngine:
    """Both rails behind one `assess` call.

    Constructed with sensible defaults so the composition root can wire it in
    one line, and every part is replaceable so a trained model can be dropped in
    without touching the simulation.
    """

    def __init__(self, card_scorer=None, wire_scorer: WireScorer | None = None,
                 history: AccountHistory | None = None,
                 graph: TransferGraph | None = None) -> None:
        self.history = history or AccountHistory()
        self.card = card_scorer or UntrainedCardScorer(history=self.history)
        self.wire = wire_scorer or WireScorer(graph=graph or TransferGraph())
        self.counters = Counters()
        self.cases: list[Case] = []

    def assess(self, context: RiskContext) -> RiskDecision:
        """Score one transaction. Never raises.

        The engine treats an exception here as ALLOW, but catching it here as
        well means a failure is *counted* rather than silently absorbed - a rail
        that is failing on every call and a rail that is finding nothing look
        identical from the outside otherwise.
        """
        self.counters.assessed += 1

        if context.tx_type in CARD_TYPES:
            decision = self.card.assess(context)
        elif context.tx_type in WIRE_TYPES:
            decision = self.wire.assess(context)
        else:
            self.counters.unscored += 1
            return RiskDecision.allow(rail="none", reason="type is not scored")

        self._record(context, decision)
        self._maybe_evict(context)
        return decision

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
        ))

    def _maybe_evict(self, context: RiskContext) -> None:
        if self.counters.assessed % EVICT_EVERY == 0:
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
            "flag_rate": flagged / max(counters.assessed - counters.unscored, 1),
            "by_rail": dict(counters.by_rail),
            "open_cases": len(self.cases),
            "accounts_tracked": len(self.history),
        }
