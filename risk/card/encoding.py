"""The one place FinSim's fields are mapped onto the model's schema.

Training and serving must build a feature vector the same way, byte for byte. If
they drift, the model is fed something slightly different from what it learned
on and the failure is silent: no exception, no obviously wrong number, just
worse predictions that look like a weak model rather than a bug. It is one of
the most common ways a working model breaks in production, and the defence is
structural - one module, imported by both paths, with no second implementation
anywhere.

## The schema

| role | fields |
| --- | --- |
| static (per account) | account type, KYC level |
| dynamic numerical | amount, time since last, the counts and deltas |
| dynamic categorical | transaction type, gateway, device, hour, weekday |
| network signal | fraud |

The numerical block is the part worth explaining. IEEE-CIS gave the offline model
fourteen `C` columns (counts of things associated with a card) and fifteen `D`
columns (days since various first-seen events), and the measurement that
justified this whole design kept them - they are derivable from any raw event
log, unlike the anonymised `V` block. `AccountHistory` recomputes that same kind
of quantity from FinSim's stream, and this module is where those land in the
tensor.

Categorical values are indexed from a vocabulary fitted at training time. Index
0 is reserved for unseen-or-missing, so a gateway that appears for the first
time in production takes the unknown slot rather than silently borrowing another
gateway's embedding.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from risk.card.history import MAX_SEQ_LEN, Observation

# Static attributes come from the account, not the transaction, so they are
# constant across a sequence and sit at position 0 of the model's input.
STATIC_CAT = ["account_type", "kyc_level"]

DYNAMIC_CAT = ["tx_type", "gateway_id", "device_type", "hour_of_day", "day_of_week"]

# Order is load-bearing: it defines the column order of the tensor, and a model
# trained on one order scores nonsense under another. Adding a field means
# appending, never inserting.
DYNAMIC_NUM = [
    "amount_paise",
    "time_delta_seconds",
    "distinct_destinations",
    "distinct_devices",
    "distinct_gateways",
    "txns_last_hour",
    "txns_last_day",
    "txns_last_week",
    "seconds_since_first_seen",
    "seconds_since_new_destination",
    "amount_over_account_mean",
]

UNKNOWN = 0


@dataclass
class Vocabulary:
    """Categorical value to index, fitted on training traffic only.

    Fitting on everything the model will ever see - including the evaluation
    period - lets a category that only exists in the future take a learned
    embedding, which flatters the model in a way production never will.
    """

    tables: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def fit(cls, sequences: list[list[Observation]]) -> Vocabulary:
        tables: dict[str, dict[str, int]] = {}
        for name in DYNAMIC_CAT + STATIC_CAT:
            values = sorted({str(getattr(o, name)) for seq in sequences for o in seq})
            tables[name] = {v: i + 1 for i, v in enumerate(values)}
        return cls(tables=tables)

    def index(self, name: str, value: object) -> int:
        return self.tables.get(name, {}).get(str(value), UNKNOWN)

    def cardinality(self, name: str) -> int:
        # +1 for the reserved unknown slot at index 0.
        return len(self.tables.get(name, {})) + 1

    def to_dict(self) -> dict:
        return {"tables": self.tables}

    @classmethod
    def from_dict(cls, data: dict) -> Vocabulary:
        return cls(tables=data["tables"])


def encode_sequence(sequence: list[Observation], vocab: Vocabulary,
                    max_len: int = MAX_SEQ_LEN) -> dict[str, np.ndarray]:
    """One account's history as the arrays the model reads.

    The sequence is right-aligned truncated: when an account has more history
    than the model attends over, the **most recent** transactions are kept. The
    transaction being scored is always the last real position, which is what the
    scorer reads its answer from.
    """
    recent = sequence[-max_len:]
    if not recent:
        raise ValueError("cannot encode an empty sequence")
    length = len(recent)

    # Static attributes are constant across a sequence, so any row carries
    # them. The most recent is used because it is the account as it is now.
    static_cat = np.zeros((1, len(STATIC_CAT)), dtype="int64")
    for j, name in enumerate(STATIC_CAT):
        static_cat[0, j] = vocab.index(name, getattr(recent[-1], name))

    dyn_cat = np.zeros((1, max_len, len(DYNAMIC_CAT)), dtype="int64")
    dyn_num = np.zeros((1, max_len, len(DYNAMIC_NUM)), dtype="float32")
    pad_mask = np.ones((1, max_len), dtype=bool)

    for t, observation in enumerate(recent):
        for j, name in enumerate(DYNAMIC_CAT):
            dyn_cat[0, t, j] = vocab.index(name, getattr(observation, name))
        for j, name in enumerate(DYNAMIC_NUM):
            dyn_num[0, t, j] = float(getattr(observation, name))
        pad_mask[0, t] = False

    return {"static_cat": static_cat, "dyn_cat": dyn_cat, "dyn_num": dyn_num,
            "pad_mask": pad_mask, "length": length}


def build_schema(vocab: Vocabulary):
    """The model's attribute schema, sized by the fitted vocabulary."""
    from risk.card.treasure.config import CatAttr, NumAttr, Schema

    schema = Schema()
    schema.static_num = []
    schema.static_cat = [CatAttr(n, vocab.cardinality(n)) for n in STATIC_CAT]
    schema.dynamic_num = [NumAttr(n) for n in DYNAMIC_NUM]
    schema.dynamic_cat = [CatAttr(n, vocab.cardinality(n)) for n in DYNAMIC_CAT]
    schema.signal_num = []
    schema.signal_cat = [CatAttr("is_fraud", 2)]
    return schema
