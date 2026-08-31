"""The IBM-trained wire model, as one signal rather than a second opinion.

Blending its score with the rules' was measured and lost - averaging two
rankings spreads a noisy member's error over the whole ordering to buy a small
set of genuine catches. But the two are not redundant: their rank correlation is
0.344, and of the transfers in the model's top slice that the rules ranked
lower, every one was an attacker. There is signal there; averaging is the wrong
way to take it.

So the model enters where every other piece of evidence enters - as a `Signal`
with a weight. That makes the combination **conjunctive rather than averaged**:
a high model score raises a case when something structural also fired, and on
its own it contributes without deciding. That is the shape the rest of this rail
already uses, and it is why a weak-but-different signal can help here where
blending could not.

Four of the model's 49 features are constants in a single-bank single-currency
simulator - `payment_format`, `same_bank`, `currency`, `currency_switch` - and
they carry 31.6% of its training gain, `payment_format` being its single
strongest feature. A tree splitting on a constant sends every row down one
branch, so this model runs here with its best splits inert. That is exactly why
it is a contributing signal and not the decision.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from risk.wire.graph import NANOS_PER_HOUR, AccountStructure, TransferGraph
from sim.core.interfaces import RiskContext

# What a single-bank, single-currency simulator fixes. Stated rather than
# inferred, so the reason each is constant is readable.
CONSTANTS = {
    "same_bank": 1.0,          # one bank, so every transfer is internal
    "currency_switch": 0.0,    # one currency, so nothing ever switches
    "currency": 0.0,
    "payment_format": 0.0,     # one payment path
}


class WireModel:
    """A trained booster, fed from the account graph the rules already keep.

    Built from the same `AccountStructure` the rules read, so the model and the
    rules see one graph rather than two - which also means adding it costs no
    extra bookkeeping per transfer.
    """

    def __init__(self, booster, feature_names: list[str]) -> None:
        self.booster = booster
        self.names = feature_names

    @classmethod
    def load(cls, model_path: str | Path,
             names_path: str | Path | None = None) -> WireModel:
        import xgboost as xgb

        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"no wire model at {model_path}. The rules work without one - "
                f"they score better alone than any blend measured - so this is "
                f"optional, not a missing dependency.")

        names_path = Path(names_path or model_path.with_name("wire_feature_names.json"))
        booster = xgb.Booster()
        booster.load_model(str(model_path))
        return cls(booster, json.loads(names_path.read_text(encoding="utf-8")))

    def features(self, context: RiskContext, source: AccountStructure,
                 destination: AccountStructure) -> np.ndarray:
        """The 49 features, from this graph rather than from IBM's schema."""
        amount = context.amount_paise / 100.0
        sent_mean = source.sent_total / 100.0 / max(source.sent_count, 1)

        row = dict(CONSTANTS)
        row.update({
            "amount": amount,
            "log_amount": float(np.log1p(amount)),
            "amount_gap": 0.0,
            "self_loop": float(context.source_account_id
                               == context.destination_account_id),
            "hour": float(int(context.sim_time_ns // NANOS_PER_HOUR) % 24),
            "day_of_week": float(int(context.sim_time_ns // (24 * NANOS_PER_HOUR)) % 7),
            "sent_txns": float(source.sent_count),
            "sent_partners": float(source.out_degree),
            "sent_total": source.sent_total / 100.0,
            "sent_mean": sent_mean,
            "sent_max": source.sent_total / 100.0,
            "sent_partner_ratio": source.out_degree / max(source.sent_count, 1),
            "recv_txns": float(destination.received_count),
            "recv_partners": float(destination.in_degree),
            "recv_total": destination.received_total / 100.0,
            "recv_mean": destination.received_total / 100.0
                         / max(destination.received_count, 1),
            "recv_max": destination.received_total / 100.0,
            "recv_partner_ratio": destination.in_degree
                                  / max(destination.received_count, 1),
            "sender_passthrough": source.passthrough,
            "receiver_passthrough": destination.passthrough,
            "amount_vs_sender_mean": amount / max(sent_mean, 1.0),
            "from_scatter_gather_width": float(source.fan_out_burst),
            "from_gather_scatter_width": float(source.fan_in_burst),
            "from_two_hop_payees": float(source.out_degree),
            "from_two_hop_payers": float(source.in_degree),
            "from_fan_out_burst": float(source.fan_out_burst),
            "from_out_degree": float(source.out_degree),
            "from_in_degree": float(source.in_degree),
            "from_cycle_count": float(source.cycle_count),
            "from_tight_cycle_count": float(source.cycle_count),
            "from_shortest_cycle": float(source.shortest_cycle),
            "from_fastest_cycle_hours": source.fastest_cycle_hours,
            "from_largest_cycle_value": source.sent_total / 100.0,
            "to_scatter_gather_width": float(destination.fan_out_burst),
            "to_gather_scatter_width": float(destination.fan_in_burst),
            "to_two_hop_payees": float(destination.out_degree),
            "to_two_hop_payers": float(destination.in_degree),
            "to_fan_out_burst": float(destination.fan_out_burst),
            "to_out_degree": float(destination.out_degree),
            "to_in_degree": float(destination.in_degree),
            "to_cycle_count": float(destination.cycle_count),
            "to_tight_cycle_count": float(destination.cycle_count),
            "to_shortest_cycle": float(destination.shortest_cycle),
            "to_fastest_cycle_hours": destination.fastest_cycle_hours,
            "to_largest_cycle_value": destination.received_total / 100.0,
        })
        return np.array([[row[name] for name in self.names]], dtype="float32")

    def score(self, context: RiskContext, source: AccountStructure,
              destination: AccountStructure) -> float:
        import xgboost as xgb

        matrix = xgb.DMatrix(self.features(context, source, destination),
                             feature_names=self.names)
        return float(self.booster.predict(matrix)[0])
