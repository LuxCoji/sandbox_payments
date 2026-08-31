"""The card rail: the seam a trained model plugs into, and what runs before one.

## Why there is no model file here yet

The card model measured on IEEE-CIS reaches 33.2% recall at a 2% flag rate using
only the fields a payment simulator can supply - 2.4 times what a per-row model
manages on the same fields. That result is what justified choosing a sequence
model over the gradient boosted card rail.

It is **not** a model that can be copied into FinSim. The offline version was
given `card1` and `card2` as inputs, which are card identifiers: it learned one
embedding per card number in that dataset, and a FinSim account id has no entry
in that table. Whether those columns matter is being measured; the paper itself
never feeds card identifiers to the model, using the card only to group
transactions into a sequence, so the expectation is that they can be dropped and
the weights transferred.

Until that is settled, this module ships two implementations and is honest about
which one is running:

  `UntrainedCardScorer` - allows everything, and says so on construction. This
      is what runs before a model exists. It is not a fallback that quietly
      approximates the model; it does nothing at all, deliberately, because a
      hand-written stand-in that scores 5% would be indistinguishable from a
      broken model at a glance and would make the integration look finished
      when it is not.

  `SequenceCardScorer` - wraps a trained model and the account history it reads
      from. Requires weights; refuses to start without them rather than
      degrading silently.

Both satisfy the same protocol, so wiring one or the other changes nothing
elsewhere.
"""
from __future__ import annotations

import logging
from typing import Protocol

from risk.card.history import AccountHistory, Observation
from risk.thresholds import AmountAwareBands
from sim.core.interfaces import RiskAction, RiskContext, RiskDecision

RAIL = "card"
log = logging.getLogger(__name__)


class SequenceModel(Protocol):
    """A trained model that turns an account's recent history into a probability.

    Narrow on purpose. The scorer owns thresholds, history and the decision;
    the model only has to answer "how unusual is the last transaction in this
    sequence", which is the one thing that has to be learned rather than
    written down.
    """

    def fraud_probability(self, sequence: list[Observation]) -> float:
        ...


class UntrainedCardScorer:
    """Allows every payment. What runs until a model has been trained here.

    Exists so the seam can be wired, tested and reviewed before a model exists,
    without anything pretending to detect fraud in the meantime.
    """

    def __init__(self, history: AccountHistory | None = None) -> None:
        self.history = history or AccountHistory()
        log.warning(
            "card rail has no trained model: every payment will be allowed. "
            "Train one on simulator traffic and wire SequenceCardScorer.")

    def assess(self, context: RiskContext) -> RiskDecision:
        # The history is still maintained. It is the training data.
        self.history.observe(context)
        return RiskDecision.allow(
            rail=RAIL, reason="no card model trained for this environment")


class SequenceCardScorer:
    """Scores a payment against what the account has been doing.

    The model sees the account's recent history with this transaction appended,
    which is the same shape it was trained on. The threshold it is compared
    against moves with the amount, so a large payment needs less confidence to
    be worth stopping than a small one.
    """

    def __init__(self, model: SequenceModel,
                 history: AccountHistory | None = None,
                 bands: AmountAwareBands | None = None) -> None:
        if model is None:
            raise ValueError(
                "SequenceCardScorer needs a trained model. Use "
                "UntrainedCardScorer if none exists yet, so that the absence "
                "of a model is visible rather than silent.")
        self.model = model
        self.history = history or AccountHistory()
        self.bands = bands or AmountAwareBands()

    def assess(self, context: RiskContext) -> RiskDecision:
        # observe() returns the transaction as the model reads it and appends
        # it to the account's history, so the sequence handed to the model ends
        # with the transaction being scored.
        self.history.observe(context)
        sequence = self.history.sequence(context.source_account_id)

        score = float(self.model.fraud_probability(sequence))
        action = self.bands.decide(score, context.amount_paise)

        if action is RiskAction.ALLOW:
            return RiskDecision.allow(rail=RAIL, score=score)
        return RiskDecision(
            action=action, score=score, rail=RAIL,
            reason=f"score {score:.3f} against a "
                   f"{self.bands.step_up_threshold(context.amount_paise):.3f} "
                   f"bar for this amount",
        )
