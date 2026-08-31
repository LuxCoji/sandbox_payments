"""Noticing when a live model stops working.

A fraud model degrades quietly. Attack patterns change, traffic drifts, and the
model keeps returning confident scores that mean less every week. Nothing
throws. The alert queue stays the same size, because the threshold is a
percentile and the percentile always exists. The first real symptom is
chargebacks weeks later.

So two things are watched, and neither needs labels - which matters, because
labels arrive late in production and not at all in a live simulation:

**Score drift.** The distribution of scores the model emits, compared with what
it emitted on the traffic it was fitted against. Population Stability Index over
fixed bins.

**Flag-rate drift.** The share of traffic being flagged. The threshold is
supposed to hold this near a target; if it moves, either the traffic changed or
the model did.

Bins come from the reference distribution and are then **stored, not
recomputed**. Recomputing quantiles on the live window would move the bins with
the data, so a shifted distribution would land in shifted bins and report no
drift - which is the exact failure this is meant to catch.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

# The conventional reading of PSI: under 0.1 is noise, 0.1-0.25 is worth
# watching, above 0.25 means the input distribution has genuinely moved.
PSI_WATCH = 0.10
PSI_ALERT = 0.25

# A flag rate this far from target means the threshold no longer does what it
# was fitted to do, whatever the scores look like.
FLAG_RATE_TOLERANCE = 0.5      # relative: 2% target alerts below 1% or above 3%


@dataclass
class Reference:
    """What the model looked like when it was accepted.

    Stored alongside the model. Without it there is nothing to compare against,
    and "the scores look reasonable" is not a measurement.
    """

    bin_edges: list[float]
    bin_counts: list[float]
    flag_rate: float
    threshold: float
    sample_size: int

    @classmethod
    def fit(cls, scores: np.ndarray, threshold: float, bins: int = 10) -> Reference:
        scores = np.asarray(scores, dtype="float64")
        if len(scores) < 100:
            raise ValueError(
                f"a reference built on {len(scores)} scores is not a "
                f"distribution - collect more before accepting a model")

        # Fixed edges over the score range rather than quantiles of it. Quantile
        # edges recomputed later would move with the data and hide the drift.
        edges = np.linspace(0.0, 1.0, bins + 1)
        counts, _ = np.histogram(scores, bins=edges)
        return cls(bin_edges=edges.tolist(),
                   bin_counts=(counts / max(len(scores), 1)).tolist(),
                   flag_rate=float((scores >= threshold).mean()),
                   threshold=float(threshold), sample_size=len(scores))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> Reference:
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def population_stability_index(reference: Reference,
                               scores: np.ndarray) -> float:
    """How far the live score distribution has moved from the reference.

    Uses the reference's **stored** bin edges. An empty bin is floored rather
    than skipped: a bin that held 8% of reference traffic and now holds nothing
    is the strongest evidence of drift available, and dividing by zero would
    throw it away.
    """
    scores = np.asarray(scores, dtype="float64")
    edges = np.array(reference.bin_edges)
    counts, _ = np.histogram(scores, bins=edges)
    live = counts / max(len(scores), 1)
    ref = np.array(reference.bin_counts)

    floor = 1e-4
    live = np.maximum(live, floor)
    ref = np.maximum(ref, floor)
    return float(np.sum((live - ref) * np.log(live / ref)))


@dataclass
class DriftReport:
    psi: float
    flag_rate: float
    reference_flag_rate: float
    verdict: str
    reasons: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.verdict == "healthy"


def check(reference: Reference, scores: np.ndarray) -> DriftReport:
    """Compare a live window against the reference. Never raises.

    Returns a verdict rather than acting on one. Whether a drifting model should
    be rolled back is an operational decision with a person attached, and a
    monitor that rolled back on its own would take a rail down over a busy
    weekend.
    """
    scores = np.asarray(scores, dtype="float64")
    psi = population_stability_index(reference, scores)
    flag_rate = float((scores >= reference.threshold).mean())

    reasons = []
    verdict = "healthy"

    if psi >= PSI_ALERT:
        verdict = "alert"
        reasons.append(f"score distribution has moved (PSI {psi:.3f})")
    elif psi >= PSI_WATCH:
        verdict = "watch"
        reasons.append(f"score distribution is moving (PSI {psi:.3f})")

    target = reference.flag_rate
    if target > 0:
        relative = abs(flag_rate - target) / target
        if relative >= FLAG_RATE_TOLERANCE:
            verdict = "alert"
            reasons.append(
                f"flag rate {flag_rate:.2%} against {target:.2%} at acceptance")

    if len(scores) < 200:
        # Said out loud rather than silently returning "healthy" - a quiet
        # window is the other way a monitor lies.
        reasons.append(f"only {len(scores)} scores in this window; thin evidence")

    return DriftReport(psi=psi, flag_rate=flag_rate,
                       reference_flag_rate=target, verdict=verdict,
                       reasons=reasons)
