"""Loading a trained model and asking it about one account.

The point of this module is that putting a model into production is a single
call - `TorchSequenceModel.load(path)` - and that everything which could differ
between training and serving is carried inside the checkpoint rather than
guessed at start-up.

A checkpoint holds three things, and all three matter:

  **weights**     what was learned
  **vocabulary**  how categories were indexed, so a gateway means the same
                  integer it did during training
  **schema shape** the attribute cardinalities the weights were built for

Loading rebuilds the model from the stored schema rather than from a default,
because a mismatch there does not raise - PyTorch will happily load a state dict
into a differently-sized embedding table if the shapes happen to line up, and
the model then reads the wrong column for every category.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch

from risk.card.encoding import Vocabulary, build_schema, encode_sequence
from risk.card.history import MAX_SEQ_LEN, Observation

# The recipe settled offline: pretrain six epochs, fine-tune twenty, standard
# model size. More pretraining scored worse; a larger model scored worse.
DEFAULT_MODEL_CONFIG = {"d_model": 256, "n_layers": 3, "n_heads": 4,
                        "input_layers": 3, "max_seq_len": MAX_SEQ_LEN}


class TorchSequenceModel:
    """A trained TREASURE, ready to score one account at a time.

    Satisfies `risk.card.scorer.SequenceModel`, which is the whole interface the
    scorer depends on.
    """

    def __init__(self, model, vocab: Vocabulary, device: str = "cpu",
                 config: dict | None = None) -> None:
        self.model = model.to(device).eval()
        self.vocab = vocab
        self.device = device
        # The shape the weights were built for. Stored in the checkpoint and
        # used to rebuild on load, because a size mismatch does not raise: a
        # state dict loads happily into a differently-sized embedding table
        # whenever the shapes line up, and the model then reads the wrong column
        # for every category.
        self.config = dict(config or DEFAULT_MODEL_CONFIG)

    @torch.no_grad()
    def fraud_probability(self, sequence: list[Observation]) -> float:
        """How unusual the last transaction in this sequence looks.

        Returns 0.0 for an empty sequence rather than raising. An account with
        no history is not evidence of fraud, and a scorer that threw here would
        take down the first payment of every new account.
        """
        if not sequence:
            return 0.0

        arrays = encode_sequence(sequence, self.vocab)
        batch = {
            "static_num": torch.zeros(1, 0, device=self.device),
            "static_cat": torch.from_numpy(arrays["static_cat"]).to(self.device),
            "dyn_cat": torch.from_numpy(arrays["dyn_cat"]).to(self.device),
            "dyn_num": torch.from_numpy(arrays["dyn_num"]).to(self.device),
            "pad_mask": torch.from_numpy(arrays["pad_mask"]).to(self.device),
        }

        out = self.model(batch)
        head = out["signal"]["cats"][0]
        logits = head["H_a"] @ head["E"].T
        probabilities = torch.softmax(logits, dim=-1)[..., 1]

        # The answer is at the last real position - the transaction being
        # scored. Reading position 0, or a mean over the sequence, would answer
        # a different question: how unusual the account has been overall.
        last = arrays["length"] - 1
        return float(probabilities[0, last].item())

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "state_dict": self.model.state_dict(),
            "vocabulary": self.vocab.to_dict(),
            "model_config": self.config,
        }, path)
        return path

    @classmethod
    def load(cls, path: str | Path, device: str = "cpu") -> TorchSequenceModel:
        """Rebuild a saved model. Everything needed comes from the file."""
        from risk.card.treasure.config import ModelConfig
        from risk.card.treasure.model import build_model

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"no card model at {path}. Train one with "
                f"`python -m risk.card.training`, or wire UntrainedCardScorer "
                f"so that the absence of a model is visible rather than silent.")

        checkpoint = torch.load(path, map_location=device, weights_only=False)
        vocab = Vocabulary.from_dict(checkpoint["vocabulary"])
        model = build_model(build_schema(vocab),
                            ModelConfig(**checkpoint.get("model_config",
                                                         DEFAULT_MODEL_CONFIG)))
        model.load_state_dict(checkpoint["state_dict"])
        return cls(model, vocab, device=device)


def score_all(model: TorchSequenceModel,
              sequences: list[list[Observation]]) -> np.ndarray:
    """Score many sequences, for evaluation rather than serving.

    Serving scores one account per payment. Evaluation scores a whole held-out
    period, and this is the loop that does it.
    """
    return np.array([model.fraud_probability(s) for s in sequences],
                    dtype="float64")
