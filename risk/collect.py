"""Writing down what the rails saw, so a model can be trained on it.

The card rail ships without a model. What makes that recoverable rather than
permanent is that the history is maintained anyway - every payment is reduced to
the exact record the model would read, and this module appends it to a file.
Run the simulator, run the red team, and the training set builds itself.

## Labelling

Exact, not inferred. Every command carries `actor_id`, and the red-team harness
knows which actors it controls, so a transaction is fraud if and only if the
attacker made it. There is no hand labelling and no heuristic, which removes the
single largest source of noise in most fraud datasets.

The attacker's identities are passed in rather than detected. A recorder that
tried to work out for itself who the attacker was would be a fraud model, and
using one model's guesses as another model's ground truth is how a system learns
to agree with itself.

## Format

JSON Lines, appended. A long collection run can be interrupted without losing
what came before: a half-written array is unreadable, a half-written line costs
one transaction.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from risk.card.history import Observation
from sim.core.interfaces import RiskContext


class TrafficRecorder:
    """Appends one line per scored transaction.

    Opened lazily and kept open: reopening per transaction would dominate the
    cost of scoring in a long run.
    """

    def __init__(self, path: str | Path,
                 attacker_actor_ids: set[str] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.attackers = set(attacker_actor_ids or ())
        self._handle = None
        self.written = 0
        self.fraud_written = 0

    def mark_attacker(self, actor_id: str) -> None:
        """Tell the recorder about an attacker identity as the harness creates it."""
        self.attackers.add(actor_id)

    def record(self, context: RiskContext, observation: Observation) -> None:
        if self._handle is None:
            self._handle = self.path.open("a", encoding="utf-8")

        is_fraud = int(context.actor_id in self.attackers)
        self._handle.write(json.dumps({
            "account_id": context.source_account_id,
            "tx_id": context.tx_id,
            "actor_id": context.actor_id,
            "is_fraud": is_fraud,
            "observation": asdict(observation),
        }) + "\n")

        self.written += 1
        self.fraud_written += is_fraud
        # Flushed periodically rather than per line: a crash costs at most the
        # last few hundred transactions, and flushing every line makes
        # collection roughly an order of magnitude slower.
        if self.written % 500 == 0:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None

    def summary(self) -> dict:
        return {"path": str(self.path), "written": self.written,
                "fraud": self.fraud_written,
                "fraud_rate": self.fraud_written / max(self.written, 1),
                "attackers": sorted(self.attackers)}

    def __enter__(self) -> TrafficRecorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
