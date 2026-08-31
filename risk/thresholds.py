"""How high a score has to be before the system acts, and why it depends on size.

A single fixed threshold treats a 50-rupee payment and a 5-lakh payment as the
same decision. They are not. Being wrong about the small one costs a customer
ten seconds of friction; being wrong about the large one costs five lakh.

Elkan's rule gives the correction directly. Under a cost matrix the optimal
boundary is

    threshold = cost(false positive) / (cost(false positive) + cost(false negative))

The cost of missing fraud is the amount at risk, which varies per transaction.
The cost of a false positive is roughly fixed - the friction of a challenge, or
the margin lost on a decline. So:

    threshold(amount) = c_fp / (c_fp + amount)

Large transactions face a lower bar. That needs no retraining: it is a decision
rule over scores the model already produces.

Two bands rather than one, because forcing every flag to be a hard decline is
the most expensive option available - a lost sale plus an angry customer. A
step-up challenge is cheap friction for a genuine customer and a real obstacle
for someone holding stolen credentials, so the block budget is spent only where
confidence is very high and everything ambiguous is pushed into step-up.
"""
from __future__ import annotations

from dataclasses import dataclass

from sim.core.interfaces import RiskAction

# One rupee is a hundred paise. Amounts arrive in paise everywhere in FinSim,
# and mixing the two units silently scales every threshold by a hundred.
PAISE_PER_RUPEE = 100


@dataclass(frozen=True)
class AmountAwareBands:
    """Two thresholds, both of which move with the amount at risk.

    `false_positive_cost_paise` is the only parameter with a real-world meaning:
    what one wrongly stopped transaction costs. It sets where the whole curve
    sits - large makes the system cautious everywhere, small makes it
    aggressive - and it is the thing to calibrate against review capacity.

    `block_multiplier` raises the block bar above the step-up bar. Blocking asks
    for more confidence than challenging, so it demands a higher score at every
    amount.
    """

    false_positive_cost_paise: float = 50_000.0     # 500 rupees
    block_multiplier: float = 1.6
    floor: float = 0.05
    ceiling: float = 0.99

    def step_up_threshold(self, amount_paise: int) -> float:
        """The bar a score must clear to trigger a challenge."""
        cost = self.false_positive_cost_paise
        raw = cost / (cost + max(amount_paise, 0))
        return _clamp(raw, self.floor, self.ceiling)

    def block_threshold(self, amount_paise: int) -> float:
        """The bar for a hard decline. Always at or above the step-up bar."""
        raw = self.step_up_threshold(amount_paise) * self.block_multiplier
        return _clamp(raw, self.floor, self.ceiling)

    def decide(self, score: float, amount_paise: int) -> RiskAction:
        """Which of the three card-rail outcomes this score and amount imply.

        Order matters: block is checked first, because at large amounts the two
        thresholds converge and a score can clear both.
        """
        if score >= self.block_threshold(amount_paise):
            return RiskAction.BLOCK
        if score >= self.step_up_threshold(amount_paise):
            return RiskAction.STEP_UP
        return RiskAction.ALLOW


def _clamp(value: float, low: float, high: float) -> float:
    """Bounded at both ends, and both bounds matter.

    Without a floor, a large enough amount drives the threshold to zero and
    every transaction of that size is stopped regardless of what the model
    said. Without a ceiling, a near-zero amount is never stopped at all - which
    is exactly how card testing works, where a fraudster validates stolen cards
    with one-rupee charges before using them.
    """
    return max(low, min(high, value))
