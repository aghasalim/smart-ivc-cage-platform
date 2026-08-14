"""Standalone inference CLI — useful for spot-checking model output.

Usage:

    python -m ai.inference.predict --hour 03 --movement 0.2

prints the predicted label.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = HERE.parent / "models" / "behaviour.pkl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--hour", type=float, required=True)
    parser.add_argument("--movement", type=float, default=2.0)
    parser.add_argument("--weight", type=float, default=25.0)
    parser.add_argument("--gate-open", action="store_true")
    parser.add_argument("--grid", type=int, nargs=16, default=[0]*16)
    parser.add_argument("--minute", type=int, default=0)
    args = parser.parse_args()

    model = joblib.load(args.model)
    x = np.array(args.grid + [args.movement, args.weight, int(args.gate_open), args.hour, args.minute], dtype=float).reshape(1, -1)
    print(model.predict(x)[0])


if __name__ == "__main__":
    main()
