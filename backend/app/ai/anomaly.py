"""Streaming anomaly detector — second AI model.

Complements ``classifier.BehaviourClassifier`` (a discriminative
classifier that assigns each window to one of N behaviour states) with a
**density-based, unsupervised, online** detector that flags windows whose
RER / movement / water-intake profile is statistically unusual *for that
specific cage*.

Why a second model?

  - The classifier asks *"which known behaviour is this?"* — every window
    gets a label, even an unhealthy one (which might wrongly come back as
    "active" with low confidence).
  - The anomaly detector asks *"is this behaviour normal for this animal?"*
    — catches early metabolic stress, dehydration onset, equipment drift,
    and circadian-rhythm disruption *before* it crosses a hard rule
    threshold.

Algorithm: robust per-cage z-score using **EWMA + MAD** (Median Absolute
Deviation).  We deliberately avoid mean+stdev because the median/MAD pair
is resistant to the very anomalies we're trying to detect; we deliberately
avoid Isolation Forest / one-class SVM because:

  * They require ``scikit-learn`` heavy on the Pi.
  * They need periodic retraining; streaming MAD is online.
  * For 3-feature univariate health monitoring, the gain is marginal.

The detector is **per-cage**: every animal has its own baseline, because
RER varies meaningfully between strains, ages, and individuals.
"""
from __future__ import annotations

import logging
import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

log = logging.getLogger(__name__)

# Sliding window of recent samples used to estimate the baseline.
# 240 samples × 30s aggregator tick ≈ 2 hours of history per cage per metric.
_WINDOW = 240
# Minimum samples before we trust the baseline (otherwise everything looks
# anomalous when a fresh cage comes online).
_WARMUP = 24
# |z| ≥ this → "minor" anomaly (info alert)
_Z_INFO = 3.0
# |z| ≥ this → "major" anomaly (warning alert)
_Z_WARN = 4.5
# Constant connecting MAD to σ for normally-distributed data
# (so the resulting score is comparable to a classic z-score).
_MAD_K = 1.4826


@dataclass
class _MetricState:
    samples: Deque[float] = field(default_factory=lambda: deque(maxlen=_WINDOW))

    def push(self, value: float) -> None:
        self.samples.append(float(value))

    def baseline(self) -> tuple[float, float] | None:
        """Return (median, MAD) over the current window, or None if not warm."""
        n = len(self.samples)
        if n < _WARMUP:
            return None
        s = sorted(self.samples)
        med = s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
        abs_dev = sorted(abs(v - med) for v in s)
        mad = abs_dev[n // 2] if n % 2 else 0.5 * (abs_dev[n // 2 - 1] + abs_dev[n // 2])
        return med, mad


@dataclass
class AnomalyScore:
    metric: str
    value: float
    median: float
    mad: float
    z: float          # robust z-score (0 = perfectly normal)
    severity: str     # "ok" | "info" | "warning"
    reason: str       # human-readable, e.g. "RER 1.18 vs baseline 0.85 ± 0.04"


class AnomalyDetector:
    """Thread-safe streaming detector with per-cage, per-metric state."""

    METRICS = ("rer", "movement_cm", "water_flow_ml")

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _MetricState] = {}
        self._lock = threading.Lock()

    # --- API ---------------------------------------------------------------
    def observe(self, cage_id: str, metric: str, value: float) -> AnomalyScore:
        """Push a new sample for (cage, metric) and return its anomaly score."""
        if metric not in self.METRICS:
            raise ValueError(f"unknown metric: {metric!r}")
        if not math.isfinite(value):
            return AnomalyScore(metric, float("nan"), 0.0, 0.0, 0.0, "ok", "non-finite value ignored")

        with self._lock:
            key = (cage_id, metric)
            st = self._states.setdefault(key, _MetricState())
            base = st.baseline()
            # Always update the window AFTER scoring so anomalies don't
            # pollute their own baseline immediately.
            score = self._score(metric, value, base)
            st.push(value)
            return score

    def reset(self, cage_id: str | None = None) -> None:
        """Drop state for a cage (or all cages)."""
        with self._lock:
            if cage_id is None:
                self._states.clear()
            else:
                for k in list(self._states):
                    if k[0] == cage_id:
                        del self._states[k]

    def snapshot(self) -> dict[str, dict[str, dict]]:
        """Inspectable view of current baselines — exposed via /api/v1/system."""
        out: dict[str, dict[str, dict]] = {}
        with self._lock:
            for (cage_id, metric), st in self._states.items():
                base = st.baseline()
                out.setdefault(cage_id, {})[metric] = {
                    "samples": len(st.samples),
                    "warm": base is not None,
                    "median": base[0] if base else None,
                    "mad": base[1] if base else None,
                }
        return out

    # --- internals ---------------------------------------------------------
    def _score(self, metric: str, value: float, base: tuple[float, float] | None) -> AnomalyScore:
        if base is None:
            return AnomalyScore(metric, value, 0.0, 0.0, 0.0, "ok", "warming up")

        median, mad = base
        # When MAD is near zero (very stable signal), any change looks
        # infinitely anomalous. Floor MAD so we don't false-positive on a
        # quiet stretch.
        mad_floor = self._mad_floor(metric)
        effective_mad = max(mad, mad_floor)

        z = (value - median) / (_MAD_K * effective_mad)
        abs_z = abs(z)

        if abs_z >= _Z_WARN:
            sev = "warning"
        elif abs_z >= _Z_INFO:
            sev = "info"
        else:
            sev = "ok"

        unit = {"rer": "", "movement_cm": " cm", "water_flow_ml": " mL"}[metric]
        reason = (
            f"{self._display_metric(metric)} {value:.2f}{unit} vs baseline "
            f"{median:.2f}{unit} ± {effective_mad:.2f}{unit} (z={z:+.2f})"
        )
        return AnomalyScore(metric, value, median, mad, z, sev, reason)

    @staticmethod
    def _mad_floor(metric: str) -> float:
        """Minimum dispersion to assume per metric — avoids divide-by-zero
        on quiet baselines."""
        return {"rer": 0.02, "movement_cm": 0.5, "water_flow_ml": 0.5}.get(metric, 0.1)

    @staticmethod
    def _display_metric(metric: str) -> str:
        return {"rer": "RER", "movement_cm": "Movement", "water_flow_ml": "Water flow"}.get(metric, metric)


# Module-level singleton — shared by the aggregator loop and the
# /api/v1/system/anomaly inspection endpoint.
detector = AnomalyDetector()
