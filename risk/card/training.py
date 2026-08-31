"""Training the card model on this simulator's own traffic.

Run it:

    python -m risk.card.training --traffic runs/traffic.jsonl --out models/card.pt

Once that file exists, wiring it is one line in the composition root. Nothing
else in the system changes.

## Why the model is trained here rather than copied

The offline measurement settled *what* to build and *how*: a sequence model
reaching 33.2% recall at a 2% flag rate on the fields a simulator can supply,
against 14.1% for a per-row model on the same fields, using six epochs of
self-supervised pretraining followed by twenty supervised. More pretraining
scored worse. A larger model scored worse.

What it could not settle is the weights. The offline model was given card
identifiers as inputs and learned one embedding per card number in that dataset;
a FinSim account id has no entry in that table. The architecture, the recipe and
the decision transfer. The lookup table does not.

## The two phases, and why the first one matters most

**Pretraining reads no labels.** It asks the model to predict each account's
*next* transaction from the ones before it, which teaches it what normal
behaviour looks like. That was the only lever in the offline experiments that
produced a real gain, and it is the one this environment is unusually good for:
the simulator generates unbounded unlabelled traffic, and all of it is genuinely
in the past by training time.

**Fine-tuning reads labels**, and here they are exact rather than inferred.
Every event carries `actor_id`, and the red agent's identity is known, so a
transaction is fraud if and only if the attacker made it. No hand labelling, no
guessing.

## The split

Chronological, never random. A random split lets the model learn from an
account's later behaviour and then be tested on its earlier behaviour, which
reports a number that cannot be achieved when the future has not happened yet.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from risk.card.encoding import (
    DYNAMIC_CAT,
    DYNAMIC_NUM,
    STATIC_CAT,
    Vocabulary,
    build_schema,
    encode_sequence,
)
from risk.card.history import MAX_SEQ_LEN, Observation

# The recipe, settled by measurement rather than preference. See
# reports/step17_treasure_pretrain.md in the fraud_mastercard repository.
PRETRAIN_EPOCHS = 6
FINETUNE_EPOCHS = 20
AUX_WEIGHT = 0.3
BATCH_SIZE = 256
TRAIN_FRACTION = 0.8


def load_traffic(path: Path) -> list[dict]:
    """Read collected traffic. One JSON object per line, chronological.

    JSON Lines rather than a single document so a long collection run can be
    appended to and can survive being interrupted - a half-written array is
    unreadable, a half-written line loses one transaction.
    """
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if not rows:
        raise SystemExit(f"{path} is empty - collect traffic before training")
    return rows


def to_sequences(rows: list[dict]) -> tuple[list[list[Observation]], np.ndarray]:
    """Group rows into per-account sequences, one training example per row.

    Each example is an account's history **up to and including** one
    transaction, labelled with whether that transaction was fraud. That is the
    same shape serving produces: the scorer appends the payment being judged and
    asks about the last position.
    """
    by_account: dict[str, list[Observation]] = {}
    sequences: list[list[Observation]] = []
    labels: list[int] = []

    for row in rows:
        account = row["account_id"]
        observation = Observation(**row["observation"])
        history = by_account.setdefault(account, [])
        history.append(observation)
        sequences.append(list(history[-MAX_SEQ_LEN:]))
        labels.append(int(row.get("is_fraud", 0)))

    return sequences, np.array(labels, dtype="int64")


def stack(sequences: list[list[Observation]], labels: np.ndarray,
          vocab: Vocabulary) -> dict[str, np.ndarray]:
    """Encode every sequence into the batched arrays the trainer expects."""
    n = len(sequences)
    out = {
        "static_cat": np.zeros((n, len(STATIC_CAT)), dtype="int64"),
        "dyn_cat": np.zeros((n, MAX_SEQ_LEN, len(DYNAMIC_CAT)), dtype="int64"),
        "dyn_num": np.zeros((n, MAX_SEQ_LEN, len(DYNAMIC_NUM)), dtype="float32"),
        "pad_mask": np.ones((n, MAX_SEQ_LEN), dtype=bool),
        "sig_cat": np.zeros((n, MAX_SEQ_LEN, 1), dtype="int64"),
        # Which positions carry a real label. Only the last one does here, and
        # the loss must be told - every earlier position is a real transaction
        # sitting at a placeholder zero, and a loss reading `~pad_mask` would
        # train the model that those transactions are genuine. A fraudulent row
        # reappears as a non-final position in up to 31 later sequences, so it
        # was being labelled fraud once and genuine twenty-nine times.
        "label_mask": np.zeros((n, MAX_SEQ_LEN), dtype=bool),
    }
    for i, sequence in enumerate(sequences):
        arrays = encode_sequence(sequence, vocab)
        out["static_cat"][i] = arrays["static_cat"][0]
        out["dyn_cat"][i] = arrays["dyn_cat"][0]
        out["dyn_num"][i] = arrays["dyn_num"][0]
        out["pad_mask"][i] = arrays["pad_mask"][0]
        # The label belongs to the transaction being judged - the last real
        # position. Labelling every position with it would tell the model that
        # an account's whole history was fraudulent because its last payment
        # was; leaving the rest at zero *and letting the loss read them* is the
        # opposite error, and is the one that shipped.
        last = arrays["length"] - 1
        out["sig_cat"][i, last, 0] = labels[i]
        out["label_mask"][i, last] = True
    return out


def train(traffic_path: Path, out_path: Path, device: str = "cpu",
          pretrain_epochs: int = PRETRAIN_EPOCHS,
          finetune_epochs: int = FINETUNE_EPOCHS,
          model_config: dict | None = None,
          warm_start: Path | None = None) -> dict:
    """Pretrain, fine-tune, save. Returns what happened, for the caller to print.

    The epoch counts and model size default to the recipe settled by
    measurement. They are arguments rather than constants only so a test can run
    the whole path in seconds; changing them for a real run means departing from
    the one configuration that was actually measured.
    """
    import torch

    from risk.card.model import DEFAULT_MODEL_CONFIG, TorchSequenceModel
    from risk.card.treasure.config import ModelConfig
    from risk.card.treasure.model import build_model
    from risk.card.treasure.train import Arm, finetune, pretrain

    rows = load_traffic(traffic_path)
    sequences, labels = to_sequences(rows)

    # Chronological, never random: the rows arrive in time order and the split
    # respects it, so the model is never tested on a period it trained through.
    cut = int(len(sequences) * TRAIN_FRACTION)
    if cut == 0 or cut == len(sequences):
        raise SystemExit(
            f"{len(sequences)} transactions is not enough to split into train "
            f"and holdout - collect more traffic")

    train_sequences, holdout_sequences = sequences[:cut], sequences[cut:]
    train_labels, holdout_labels = labels[:cut], labels[cut:]

    fraud = int(train_labels.sum())
    print(f"{len(sequences):,} transactions, {int(labels.sum()):,} fraud "
          f"({labels.mean():.3%})")
    print(f"train {len(train_sequences):,}  holdout {len(holdout_sequences):,}")
    if fraud == 0:
        raise SystemExit(
            "no fraud in the training period. Run the red team, or the "
            "scripted patterns, before fine-tuning - the supervised phase has "
            "nothing to learn from.")

    # Fitted on the training period only. A vocabulary fitted on everything
    # gives a gateway that first appears later a learned embedding, which
    # flatters the model in a way production never will.
    vocab = Vocabulary.fit(train_sequences)
    train_arrays = stack(train_sequences, train_labels, vocab)

    config = dict(DEFAULT_MODEL_CONFIG if model_config is None else model_config)
    model = build_model(build_schema(vocab), ModelConfig(**config))

    warm = None
    if warm_start is not None:
        from risk.card.warmstart import load_body

        warm = load_body(model, warm_start, device)
        print(f"warm start from {warm['trained_on']}: {warm['copied']} decoder "
              f"tensors copied, {warm['skipped_schema_dependent']} "
              f"schema-dependent ones rebuilt")
        if warm["copied"] == 0:
            raise SystemExit(
                "the warm start copied nothing - the checkpoint's model config "
                "probably differs from this one. Continuing would train from "
                "random while reporting a warm start, which is worse than not "
                "attempting one.")

    print(f"\npretraining {pretrain_epochs} epochs (no labels read)")
    arm = Arm(epochs=finetune_epochs, pretrain_epochs=pretrain_epochs,
              aux_weight=AUX_WEIGHT, batch_size=BATCH_SIZE,
              d_model=config["d_model"], n_layers=config["n_layers"],
              n_heads=config["n_heads"])
    pretrain(model, train_arrays, arm, device)

    print(f"\nfine-tuning {finetune_epochs} epochs")
    finetune(model, train_arrays, arm, device)

    trained = TorchSequenceModel(model, vocab, device=device, config=config)
    saved = trained.save(out_path)
    print(f"\nsaved {saved}")

    holdout = _evaluate(trained, holdout_sequences, holdout_labels)
    print(f"holdout: {holdout['fraud']:,} fraud in {holdout['rows']:,} rows, "
          f"recall@2% {holdout['recall_at_2pct']:.2%}")
    return {"path": str(saved), "warm_start": warm, **holdout}


def _evaluate(model, sequences: list[list[Observation]],
              labels: np.ndarray) -> dict:
    """Recall at a 2% flag rate on the held-out period.

    The same operating point every offline measurement used, so the two numbers
    can be compared. It is not the same *task* - different traffic, different
    fraud - so a gap between them is expected and is not evidence of a bug.
    """
    from risk.card.model import score_all

    if labels.sum() == 0:
        return {"rows": len(labels), "fraud": 0, "recall_at_2pct": 0.0,
                "note": "no fraud in the holdout period"}

    scores = score_all(model, sequences)
    k = max(1, int(len(scores) * 0.02))
    flagged = np.argsort(-scores)[:k]
    return {
        "rows": len(labels),
        "fraud": int(labels.sum()),
        "recall_at_2pct": float(labels[flagged].sum() / labels.sum()),
        "precision_at_2pct": float(labels[flagged].sum() / k),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traffic", type=Path, required=True,
                        help="JSON Lines traffic collected from the simulator")
    parser.add_argument("--out", type=Path, default=Path("models/card.pt"))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--warm-start", type=Path, default=None,
                        dest="warm_start",
                        help="Checkpoint to copy the decoder body from. The "
                             "input and output modules are always rebuilt - "
                             "they are sized by this environment's schema.")
    args = parser.parse_args()

    result = train(args.traffic, args.out, args.device,
                   warm_start=args.warm_start)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
