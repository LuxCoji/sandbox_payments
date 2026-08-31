"""Starting from weights trained elsewhere, instead of from random.

The offline model was trained on IEEE-CIS: 590,540 real transactions with real
fraud labels. FinSim will not have that much traffic for a long time, and what
it has is synthetic. So the question is whether the offline model's weights are
worth anything here.

**Part of it transfers and part of it cannot**, and the split is structural
rather than a judgement call:

  input module    embeddings sized by IEEE-CIS's vocabulary. `ProductCD` has
                  five values there and does not exist here at all. Cannot
                  transfer - the tensors are the wrong shape and their columns
                  mean different things.

  decoder body    attention blocks and the final norm. Their shapes depend only
                  on d_model, n_layers and n_heads. **This is what transfers**,
                  and it is where the model learned what a spending trajectory
                  looks like - bursts, quiet periods, escalation.

  output heads    sized by the schema again. Cannot transfer.

This is the vocabulary-swap transfer that language models use to move between
languages: keep the body, rebuild the embeddings, fine-tune. The measurement
that made it plausible here is that dropping the card identifier columns cost
1.70 points against 1.82 points of seed spread - the model does not depend on
knowing particular cards, so the body is not memorising identities.

**Whether it actually helps is unmeasured.** It is a warm start, not a free
model: the body arrives knowing what trajectories look like in one payment
network, and fine-tuning has to teach it this one. The honest claim is that it
saves the pretraining phase, not that it improves the result.
"""
from __future__ import annotations

from pathlib import Path

import torch

# Only these prefixes are schema-independent. Everything else is sized by the
# attribute vocabulary and would either fail to load or, worse, load into a
# same-shaped tensor whose columns mean something different.
TRANSFERABLE_PREFIXES = ("blocks.", "final_norm.")


def load_body(model, checkpoint_path: str | Path,
              device: str = "cpu") -> dict:
    """Copy the transferable weights into a freshly built model.

    Returns what happened, rather than logging it: which tensors were taken,
    which were skipped, and why. A warm start that silently copied nothing looks
    exactly like one that worked, and the difference only shows up as a model
    that trains slightly slower than expected.
    """
    checkpoint_path = Path(checkpoint_path)
    if not checkpoint_path.exists():
        raise FileNotFoundError(
            f"no pretrained checkpoint at {checkpoint_path}. Train from scratch "
            f"instead - a warm start is an optimisation, not a requirement.")

    checkpoint = torch.load(checkpoint_path, map_location=device,
                            weights_only=False)
    source = checkpoint["state_dict"]
    target = model.state_dict()

    copied, skipped_shape, skipped_schema = [], [], []
    for name, tensor in source.items():
        if not name.startswith(TRANSFERABLE_PREFIXES):
            skipped_schema.append(name)
            continue
        if name not in target:
            skipped_schema.append(name)
            continue
        if target[name].shape != tensor.shape:
            # A different d_model or layer count. Refused rather than resized,
            # because a silently truncated attention weight is worse than no
            # warm start at all.
            skipped_shape.append(name)
            continue
        target[name] = tensor.to(device)
        copied.append(name)

    model.load_state_dict(target)

    return {
        "checkpoint": str(checkpoint_path),
        "trained_on": checkpoint.get("trained_on", "unknown"),
        "source_measured": checkpoint.get("measured", {}),
        "copied": len(copied),
        "skipped_schema_dependent": len(skipped_schema),
        "skipped_shape_mismatch": len(skipped_shape),
        "shape_mismatches": skipped_shape[:5],
    }


def describe(checkpoint_path: str | Path) -> dict:
    """What a checkpoint holds, without building a model to find out."""
    checkpoint = torch.load(Path(checkpoint_path), map_location="cpu",
                            weights_only=False)
    state = checkpoint["state_dict"]
    transferable = [k for k in state if k.startswith(TRANSFERABLE_PREFIXES)]
    return {
        "tensors": len(state),
        "transferable": len(transferable),
        "model_config": checkpoint.get("model_config", {}),
        "recipe": checkpoint.get("recipe", {}),
        "measured": checkpoint.get("measured", {}),
        "trained_on": checkpoint.get("trained_on", "unknown"),
    }
