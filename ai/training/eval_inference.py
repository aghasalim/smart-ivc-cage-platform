"""Sanity check: load the trained artefact and run a single inference."""
from __future__ import annotations

import joblib
import numpy as np
from pathlib import Path

HERE = Path(__file__).resolve().parent
ART = HERE.parent / "models" / "behaviour.pkl"


def main() -> None:
    if not ART.exists():
        raise SystemExit(f"model artefact missing: {ART}")
    model = joblib.load(ART)
    # one "sleeping" looking row
    sample = np.array([
        1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,  # grid
        0.1,        # movement
        25.0,       # weight
        0,          # gate
        3,          # hour
        15,         # minute
    ], dtype=float).reshape(1, -1)
    pred = model.predict(sample)[0]
    print(f"predicted: {pred}")


if __name__ == "__main__":
    main()
