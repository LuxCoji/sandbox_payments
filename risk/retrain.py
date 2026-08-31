"""Retrain from accumulated traffic, and replace the live model only if better.

This is what the dashboard's retrain button calls. The sequence matters more
than any part of it:

  1. read the traffic collected since the model went live
  2. split it chronologically - the recent period is the holdout
  3. train a candidate on the earlier part
  4. score **both** the candidate and the live model on the holdout
  5. promote the candidate only if it wins by more than noise

Step 4 is the one that is usually skipped, and skipping it turns "retrain" into
"replace a measured model with an unmeasured one". The live model is scored on
the same held-out period at the same moment, so the comparison is between two
models on one sample rather than between two numbers from different runs.

The holdout is the *most recent* traffic, not a random slice. A model is going
to meet next week, so it should be judged on last week - and a random split lets
it learn from an account's later behaviour before being tested on its earlier
behaviour, which reports a number production will never reproduce.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from risk.card.encoding import Vocabulary
from risk.card.training import TRAIN_FRACTION, load_traffic, stack, to_sequences
from risk.registry import Registry, recall_at

# Below this there is not enough signal to justify replacing a working model.
# Retraining on a handful of frauds produces a candidate whose measured recall
# is mostly sampling noise.
MIN_ROWS = 2_000
MIN_FRAUD = 50


class NotEnoughData(Exception):
    """Raised rather than training on a sample too small to mean anything."""


def retrain(traffic_path: str | Path, models_root: str | Path,
            device: str = "cpu", **train_kwargs) -> dict:
    """Train a candidate, measure it against the live model, promote if better.

    Returns what happened - including a refusal - so the caller can show it. A
    retrain that declines to promote is a successful outcome, not an error: it
    means the model in production is still the best one measured.
    """
    from risk.card.model import TorchSequenceModel, score_all
    from risk.card.training import train

    traffic_path = Path(traffic_path)
    registry = Registry.load(models_root)

    rows = load_traffic(traffic_path)
    fraud = sum(int(r.get("is_fraud", 0)) for r in rows)
    if len(rows) < MIN_ROWS or fraud < MIN_FRAUD:
        raise NotEnoughData(
            f"{len(rows):,} transactions with {fraud} fraud. Retraining needs "
            f"at least {MIN_ROWS:,} and {MIN_FRAUD} - below that a candidate's "
            f"measured recall is mostly sampling noise, and replacing a working "
            f"model on it is a coin flip.")

    sequences, labels = to_sequences(rows)
    cut = int(len(sequences) * TRAIN_FRACTION)
    holdout_sequences, holdout_labels = sequences[cut:], labels[cut:]

    if holdout_labels.sum() == 0:
        raise NotEnoughData(
            "the holdout period contains no fraud, so nothing can be measured "
            "on it. Collect traffic that spans an attack.")

    with tempfile.TemporaryDirectory() as tmp:
        candidate_path = Path(tmp) / "candidate.pt"
        train(traffic_path, candidate_path, device=device, **train_kwargs)
        candidate = TorchSequenceModel.load(candidate_path, device=device)

        candidate_recall = recall_at(
            holdout_labels, score_all(candidate, holdout_sequences))

        # The live model is scored on the same holdout at the same moment. Its
        # recorded recall came from a different period and a different
        # vocabulary, so comparing against that number would be comparing two
        # measurements rather than two models.
        live_recall = None
        if registry.live_path.exists():
            live = TorchSequenceModel.load(registry.live_path, device=device)
            live_recall = recall_at(
                holdout_labels, score_all(live, holdout_sequences))

        record = registry.consider(candidate_path, candidate_recall,
                                   rows=len(rows), fraud=fraud,
                                   live_recall=live_recall)

    return {
        "promoted": record.promoted,
        "reason": record.reason,
        "version": record.version,
        "candidate_recall": candidate_recall,
        "live_recall_on_same_holdout": live_recall,
        "rows": len(rows),
        "fraud": fraud,
        "holdout_rows": len(holdout_labels),
        "holdout_fraud": int(holdout_labels.sum()),
    }
