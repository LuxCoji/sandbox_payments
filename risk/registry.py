"""Model versions, and the gate a new one has to clear to replace the old.

A model that improves itself from live traffic is the obvious thing to want and
the wrong thing to build. Two reasons, both of which have sunk real systems:

**A model that learns from its own decisions can be taught.** If "allowed" is
treated as "genuine", then every fraud that gets through becomes a training
example saying that shape is fine - so an attacker who finds one working attack
can widen it by repetition. The system is then being trained by the person it is
defending against.

**A blocked transaction never reveals whether it was fraud.** The model only
ever learns from what it let through, which biases it toward whatever it already
believed. That drift is invisible: the loss falls, the flag rate holds, and
recall quietly decays.

So this does not learn continuously. It retrains from accumulated *labelled*
traffic, measures the candidate against the model in production on a period
neither has trained on, and promotes only if the candidate wins. The current
model stays on disk either way, so a promotion can be undone.

The comparison is the whole point. Without it "retrain" means "replace a
measured model with an unmeasured one and hope".
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# A candidate must beat the incumbent by more than this to be promoted.
# Retraining on slightly different data moves recall by a fraction of a point in
# either direction for no reason at all, and promoting on that noise means the
# production model is chosen by a coin flip.
MIN_IMPROVEMENT = 0.01          # one point of recall

# The operating point the comparison is made at, matching every other
# measurement in this project.
FLAG_RATE = 0.02


@dataclass
class Version:
    """One trained model and what it measured when it was accepted."""

    version: int
    path: str
    trained_at: str
    rows: int
    fraud: int
    recall_at_2pct: float
    promoted: bool
    reason: str = ""
    replaced_version: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Registry:
    """Every model trained here, and which one is live.

    JSON on disk beside the models. A registry that lives only in memory forgets
    what the current model scored the moment the process restarts, and then
    there is nothing to compare a candidate against.
    """

    root: Path
    versions: list[Version] = field(default_factory=list)

    @property
    def index_path(self) -> Path:
        return self.root / "registry.json"

    @property
    def live_path(self) -> Path:
        return self.root / "card.pt"

    @classmethod
    def load(cls, root: str | Path) -> Registry:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        index = root / "registry.json"
        if not index.exists():
            return cls(root=root)
        raw = json.loads(index.read_text(encoding="utf-8"))
        return cls(root=root,
                   versions=[Version(**v) for v in raw.get("versions", [])])

    def save(self) -> None:
        self.index_path.write_text(
            json.dumps({"versions": [v.to_dict() for v in self.versions]},
                       indent=2), encoding="utf-8")

    @property
    def live(self) -> Version | None:
        """The promoted model, or None if nothing has been promoted."""
        promoted = [v for v in self.versions if v.promoted]
        return promoted[-1] if promoted else None

    def next_version(self) -> int:
        return max((v.version for v in self.versions), default=0) + 1

    def consider(self, candidate_path: Path, candidate_recall: float,
                 rows: int, fraud: int,
                 live_recall: float | None = None) -> Version:
        """Promote the candidate if it beats what is live. Record either way.

        `live_recall` must be the live model's score **on the same holdout the
        candidate was scored on**, measured at the same moment. The recall
        stored on a Version came from a different period against a different
        vocabulary; gating on it compares two measurements rather than two
        models, and a candidate can then win or lose because the traffic moved
        rather than because it is better.

        A rejected candidate is kept on disk and in the index. It is evidence
        about what does not work, and deleting it means the same idea is tried
        again in three weeks with nothing to show it was already measured.
        """
        live = self.live
        version = self.next_version()
        stored = self.root / f"card-v{version}.pt"
        shutil.copy2(candidate_path, stored)

        if live is None or live_recall is None:
            promoted = True
            reason = ("first model - nothing to compare against" if live is None
                      else "no live model on disk to score against")
        elif candidate_recall > live_recall + MIN_IMPROVEMENT:
            promoted = True
            reason = (f"beat v{live.version} on the same holdout by "
                      f"{(candidate_recall - live_recall):.1%}")
        else:
            promoted = False
            reason = (f"did not beat v{live.version} on the same holdout "
                      f"({candidate_recall:.1%} against {live_recall:.1%}, "
                      f"needs {MIN_IMPROVEMENT:.0%} more)")

        record = Version(
            version=version, path=str(stored),
            trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            rows=rows, fraud=fraud, recall_at_2pct=candidate_recall,
            promoted=promoted, reason=reason,
            replaced_version=live.version if (promoted and live) else None,
        )
        self.versions.append(record)

        if promoted:
            shutil.copy2(stored, self.live_path)

        self.save()
        return record

    def rollback(self) -> Version | None:
        """Put the previous promoted model back.

        A promotion that looked right on a holdout can still be wrong in
        production - the holdout is a period, not the future. Rolling back is
        copying a file, and it is the reason rejected versions are kept.
        """
        promoted = [v for v in self.versions if v.promoted]
        if len(promoted) < 2:
            return None

        current, previous = promoted[-1], promoted[-2]
        shutil.copy2(previous.path, self.live_path)
        current.promoted = False
        current.reason += " (rolled back)"
        self.save()
        return previous

    def summary(self) -> dict:
        live = self.live
        return {
            "versions": len(self.versions),
            "live_version": live.version if live else None,
            "live_recall": live.recall_at_2pct if live else None,
            "can_rollback": sum(1 for v in self.versions if v.promoted) >= 2,
            "history": [v.to_dict() for v in self.versions[-10:]][::-1],
        }


def recall_at(labels: np.ndarray, scores: np.ndarray,
              flag_rate: float = FLAG_RATE) -> float:
    """Share of fraud caught inside the flag budget.

    Returns 0.0 when the sample holds no fraud, rather than dividing by zero.
    A period with nothing to catch says nothing about a model, and a candidate
    must not be promoted on the strength of an empty holdout.
    """
    total = float(labels.sum())
    if total == 0:
        return 0.0
    k = max(1, int(len(scores) * flag_rate))
    caught = labels[np.argsort(-scores)[:k]].sum()
    return float(caught / total)
