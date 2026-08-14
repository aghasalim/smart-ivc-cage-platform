"""Behaviour classifier.

In production this loads ``ai/models/behaviour.pkl`` (the artefact produced by
``ai/training/train.py``).  When the artefact is missing — for example during
the first boot of a brand-new clone — we fall back to a deterministic
rule-based classifier so the platform stays useful without retraining.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    import joblib
    import numpy as np
    _ML_AVAILABLE = True
except ImportError:  # Pi deployment without scikit-learn/numpy
    _ML_AVAILABLE = False

log = logging.getLogger(__name__)

LABELS = ("sleeping", "resting", "active", "exploring", "eating", "drinking", "stereotyped")


def _featurise(f: dict[str, Any]) -> list[float]:
    grid = f["grid"]
    if len(grid) != 16:
        grid = (list(grid) + [0] * 16)[:16]
    return [*grid, float(f["movement"]), float(f["weight"]), float(f["gate_open"]),
            float(f["hour"]), float(f["minute"])]


class _RuleFallback:
    """Deterministic backup used when no trained model artefact is present."""

    def predict(self, features: dict[str, Any]) -> tuple[str, float]:
        movement = features["movement"]
        hour = features["hour"]
        gate_open = features["gate_open"]
        active_cells = sum(1 for c in features["grid"] if c > 0)

        if gate_open and movement > 5:
            return "eating", 0.78
        if movement < 1 and 0 <= hour < 6:
            return "sleeping", 0.92
        if movement < 1:
            return "resting", 0.74
        if active_cells >= 6:
            return "exploring", 0.71
        if movement > 8 and active_cells <= 2:
            return "stereotyped", 0.62
        return "active", 0.68


class BehaviourClassifier:
    def __init__(self, model_path: str | None = None) -> None:
        self.model = None
        candidate = model_path or os.environ.get("AI_MODEL_PATH")
        if not candidate:
            here = Path(__file__).resolve().parents[2] / "models" / "behaviour.pkl"
            candidate = str(here)
        if not _ML_AVAILABLE:
            log.info({"event": "ai.ml_unavailable_using_fallback"})
        elif Path(candidate).exists():
            try:
                self.model = joblib.load(candidate)
                log.info({"event": "ai.model_loaded", "path": candidate})
            except Exception as exc:  # noqa: BLE001
                log.warning({"event": "ai.model_load_failed", "path": candidate, "error": str(exc)})
                self.model = None
        else:
            log.info({"event": "ai.model_missing_using_fallback", "path": candidate})
        self._fallback = _RuleFallback()

    def predict(self, features: dict[str, Any]) -> tuple[str, float]:
        if self.model is None or not _ML_AVAILABLE:
            return self._fallback.predict(features)
        x = np.array(_featurise(features), dtype=float).reshape(1, -1)
        try:
            label = str(self.model.predict(x)[0])
            proba = self.model.predict_proba(x)[0] if hasattr(self.model, "predict_proba") else None
            confidence = float(np.max(proba)) if proba is not None else 0.8
            return label, confidence
        except Exception as exc:  # noqa: BLE001
            log.warning({"event": "ai.predict_failed", "error": str(exc)})
            return self._fallback.predict(features)


classifier = BehaviourClassifier()
