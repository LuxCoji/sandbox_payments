"""Fraud detection for FinSim.

Two rails, because card fraud and money laundering are different problems with
different economics and different law:

**card** - someone is spending money that is not theirs. The loss is immediate
and bounded by the transaction. A wrong decision is recoverable: a step-up
challenge costs a genuine customer ten seconds. So this rail is allowed to stop
a payment.

**wire** - money is being moved to disguise where it came from. There is no
victim inside the transaction and nothing looks wrong in isolation; the pattern
only exists across accounts and over time. Detection runs at roughly 12%
precision, so this rail **never stops a transfer**. It queues a case, and a
named human decides whether to freeze.

The engine never imports this package. `sim.core.interfaces` defines a
`RiskScorer` protocol and the composition root injects an implementation, so
the simulation core has no dependency on any model.

Entry point: `risk.engine.FraudRiskEngine`.
"""
from risk.engine import FraudRiskEngine

__all__ = ["FraudRiskEngine"]
