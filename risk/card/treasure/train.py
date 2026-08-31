"""Training for TREASURE, with the self-supervised head switched back on.

The first run of this architecture used only the network-signal head - the one
that predicts `isFraud`. That measured 31.97% recall at a 2% flag rate, and it
threw away the mechanism the paper is actually built on.

TREASURE has two output heads. One predicts the **next transaction's**
attributes from the current position; the other predicts the **signal** at the
current position. Only the second needs labels. The first is what teaches the
encoder what a cardholder's normal trajectory looks like, and it can be trained
on any transaction stream at all - no fraud labels required.

That is the whole reason the architecture is described as a foundation model,
and it is what this module adds:

  **pretrain**  next-transaction head only, no labels touched
  **fine-tune** signal head, with the next-transaction loss kept on at a small
                weight so the encoder does not forget the trajectory it learned

The auxiliary weight is small on purpose. There are 33 numerical and 15
categorical attributes to predict for the next transaction against one binary
signal; at equal weight the fraud objective is drowned out by roughly fifty to
one.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset


def signed_log(x: torch.Tensor) -> torch.Tensor:
    """Sign-preserving log1p on the magnitude. Matches the model's input scaling."""
    return torch.sign(x) * torch.log1p(torch.abs(x))


@dataclass
class Arm:
    """Everything that changes between arms of the experiment.

    The defaults reproduce the shipped baseline: signal head only, eight epochs,
    no pretraining. Each field below is one lever, so an arm is a diff rather
    than a fork of the training loop.
    """

    epochs: int = 8
    pretrain_epochs: int = 0
    aux_weight: float = 0.0          # weight on the next-transaction loss
    batch_size: int = 256
    lr: float = 1e-4
    pretrain_lr: float = 3e-4        # no labels to overfit, so it can move faster
    weight_decay: float = 1e-2
    grad_clip: float = 1.0
    d_model: int = 256
    n_layers: int = 3
    n_heads: int = 4
    seed: int = 42
    label: str = "baseline"
    notes: str = ""


def as_dataset(seq: dict) -> TensorDataset:
    return TensorDataset(
        torch.from_numpy(seq["static_cat"]).long(),
        torch.from_numpy(seq["dyn_num"]).float(),
        torch.from_numpy(seq["dyn_cat"]).long(),
        torch.from_numpy(seq["pad_mask"]),
        torch.from_numpy(seq["sig_cat"][..., 0]).long(),
    )


def _batch_dict(sc, dn, dc, pad, device):
    return {"static_num": torch.zeros(len(sc), 0, device=device),
            "static_cat": sc, "dyn_num": dn, "dyn_cat": dc, "pad_mask": pad}


def head_logits(cat_head: dict) -> torch.Tensor:
    """Logits over one categorical attribute's classes.

    The head emits an attribute projection `H_a` and an output embedding table
    `E`; their product is the logit over classes, which for a two-class
    attribute is exactly a binary classifier.
    """
    return cat_head["H_a"] @ cat_head["E"].T


def next_transaction_loss(out_next: dict, dyn_num: torch.Tensor,
                          dyn_cat: torch.Tensor, pad: torch.Tensor) -> torch.Tensor:
    """How well does position t predict position t+1?

    No labels are read. A position counts only when both it and the position
    after it are real, so the model is never asked to predict padding - which it
    could do perfectly while learning nothing.
    """
    valid = (~pad[:, :-1]) & (~pad[:, 1:])
    if not bool(valid.any()):
        # A batch in which every sequence has a single transaction has no
        # transition to predict. Returning a plain zero here breaks backward
        # with "element 0 of tensors does not require grad", because a constant
        # is not attached to the graph - so the zero is built from a model
        # output instead, and the step becomes a no-op rather than a crash.
        return out_next["cats"][0]["H_a"].sum() * 0.0

    losses = []

    if "num_mu" in out_next:
        mu = out_next["num_mu"][:, :-1][valid]
        log_sigma = out_next["num_log_sigma"][:, :-1][valid]
        target = signed_log(dyn_num[:, 1:][valid])
        # Gaussian negative log-likelihood in log space, constant term dropped.
        losses.append((log_sigma + 0.5 * ((target - mu) / log_sigma.exp()) ** 2).mean())

    for j, head in enumerate(out_next["cats"]):
        logits = head_logits(head)[:, :-1][valid]
        losses.append(nn.functional.cross_entropy(logits, dyn_cat[:, 1:, j][valid]))

    return torch.stack(losses).mean()


def signal_loss(out_signal: dict, sig: torch.Tensor, pad: torch.Tensor,
                weight: torch.Tensor) -> torch.Tensor:
    """Weighted cross-entropy on the fraud flag at every real position.

    Fraud is a few percent of rows, so an unweighted loss is minimised by
    predicting "genuine" everywhere. The positive class is weighted by its
    inverse frequency - a loss weight inside one model, not a resampled dataset.
    """
    valid = ~pad
    logits = head_logits(out_signal["cats"][0])[valid]
    return nn.functional.cross_entropy(logits, sig[valid], weight=weight)


def positive_weight(seq: dict) -> float:
    """Inverse frequency of fraud among the real positions of a sequence set."""
    labels = seq["sig_cat"][..., 0][~seq["pad_mask"]]
    rate = float(labels.mean())
    if rate <= 0:
        raise ValueError("no positive labels in the training sequences")
    return (1 - rate) / rate


def pretrain(model, seq: dict, cfg: Arm, device: str, log=print) -> None:
    """Self-supervised phase: predict the next transaction, ignore every label.

    Runs on the training sequences only. Pretraining on rows that fall after the
    evaluation cut-off would let the model read the future, which is the one
    thing a chronological split exists to prevent.
    """
    if cfg.pretrain_epochs <= 0:
        return

    loader = DataLoader(as_dataset(seq), batch_size=cfg.batch_size,
                        shuffle=True, drop_last=False)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.pretrain_lr,
                            weight_decay=cfg.weight_decay)

    for epoch in range(1, cfg.pretrain_epochs + 1):
        model.train()
        total, seen = 0.0, 0
        for sc, dn, dc, pad, _ in loader:
            sc, dn, dc, pad = (t.to(device) for t in (sc, dn, dc, pad))
            out = model(_batch_dict(sc, dn, dc, pad, device))
            loss = next_transaction_loss(out["next"], dn, dc, pad)
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)
            opt.step()
            total += float(loss.detach()) * len(sc)
            seen += len(sc)
        log(f"    pretrain {epoch}/{cfg.pretrain_epochs}  "
            f"next-txn loss {total / max(seen, 1):.4f}")


def finetune(model, seq: dict, cfg: Arm, device: str, log=print) -> None:
    """Supervised phase: the fraud signal, with the trajectory loss kept on."""
    loader = DataLoader(as_dataset(seq), batch_size=cfg.batch_size,
                        shuffle=True, drop_last=False)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr,
                            weight_decay=cfg.weight_decay)
    weight = torch.tensor([1.0, positive_weight(seq)], device=device)

    for epoch in range(1, cfg.epochs + 1):
        model.train()
        sig_total, aux_total, seen = 0.0, 0.0, 0
        for sc, dn, dc, pad, sig in loader:
            sc, dn, dc, pad, sig = (t.to(device) for t in (sc, dn, dc, pad, sig))
            out = model(_batch_dict(sc, dn, dc, pad, device))

            main = signal_loss(out["signal"], sig, pad, weight)
            if cfg.aux_weight > 0:
                aux = next_transaction_loss(out["next"], dn, dc, pad)
                loss = main + cfg.aux_weight * aux
                aux_total += float(aux.detach()) * len(sc)
            else:
                loss = main

            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)
            opt.step()
            sig_total += float(main.detach()) * len(sc)
            seen += len(sc)

        line = (f"    epoch {epoch}/{cfg.epochs}  "
                f"fraud loss {sig_total / max(seen, 1):.4f}")
        if cfg.aux_weight > 0:
            line += f"  next-txn {aux_total / max(seen, 1):.4f}"
        log(line)


@torch.no_grad()
def score(model, seq: dict, cfg: Arm, device: str) -> np.ndarray:
    """Fraud probability for every real position, in sequence order.

    The caller matches these to labels through `seq["row_index"]`, masked the
    same way. Both use `~pad_mask` over the same array in the same order, so the
    two line up position for position.
    """
    model.eval()
    loader = DataLoader(as_dataset(seq), batch_size=cfg.batch_size * 2)
    out_scores = []
    for sc, dn, dc, pad, _ in loader:
        sc, dn, dc, pad = (t.to(device) for t in (sc, dn, dc, pad))
        out = model(_batch_dict(sc, dn, dc, pad, device))
        probs = torch.softmax(head_logits(out["signal"]["cats"][0]), dim=-1)[..., 1]
        keep = (~pad).cpu().numpy()
        out_scores.append(probs.cpu().numpy()[keep])
    return np.concatenate(out_scores)
