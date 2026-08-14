"""Generate a synthetic training set for the behaviour classifier.

Each row is one minute of features:

    [grid_cell_0, ..., grid_cell_15, movement_cm, weight_g, gate_open, hour, minute, label]

Labels are seeded by a hand-crafted distribution that matches published
mouse-behaviour literature (mice are nocturnal, eat in bouts, sleep mostly
during the day, occasionally show stereotyped pacing).

Output: ``ai/data/synthetic.csv``
"""
from __future__ import annotations

import argparse
import csv
import math
import random
from pathlib import Path

import numpy as np

LABELS = ("sleeping", "resting", "active", "exploring", "eating", "drinking", "stereotyped")
HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"


def _activity_at(hour: float) -> float:
    rad = ((hour - 14) / 24.0) * 2.0 * math.pi
    return max(0.0, min(1.0, 0.5 + 0.5 * math.sin(rad - math.pi / 2)))


def _pick_label(hour: float, gate_open: bool, rng: random.Random) -> str:
    activity = _activity_at(hour)
    r = rng.random()
    if gate_open and r < 0.55:
        return "eating"
    if 0 <= hour < 6 and activity < 0.25:
        return "sleeping"
    if activity < 0.15:
        return "sleeping" if rng.random() < 0.7 else "resting"
    if activity < 0.35:
        return "resting" if rng.random() < 0.6 else "active"
    if r < 0.05:
        return "stereotyped"
    if r < 0.15:
        return "drinking"
    if activity > 0.7:
        return "exploring" if rng.random() < 0.55 else "active"
    return "active"


def _features_for(label: str, hour: float, gate_open: bool, rng: random.Random) -> list[float]:
    grid = np.zeros(16, dtype=int)
    if label == "sleeping":
        grid[rng.choice([0, 3, 12, 15])] = 1
        movement = rng.uniform(0.0, 0.3)
    elif label == "resting":
        grid[rng.choice(range(16))] = 1
        movement = rng.uniform(0.1, 1.0)
    elif label == "active":
        for _ in range(rng.randint(2, 5)):
            grid[rng.randint(0, 15)] += 1
        movement = rng.uniform(2.0, 6.0)
    elif label == "exploring":
        for _ in range(rng.randint(5, 10)):
            grid[rng.randint(0, 15)] += 1
        movement = rng.uniform(6.0, 14.0)
    elif label == "eating":
        # feeder is one corner — high count on cell 0 + 1
        grid[0] += rng.randint(3, 8)
        grid[1] += rng.randint(0, 3)
        movement = rng.uniform(0.5, 3.0)
    elif label == "drinking":
        grid[14] += rng.randint(3, 7)
        grid[15] += rng.randint(0, 3)
        movement = rng.uniform(0.5, 2.5)
    else:  # stereotyped
        cell = rng.randint(0, 15)
        grid[cell] += rng.randint(8, 14)
        movement = rng.uniform(7.0, 16.0)

    weight = 25.0 + rng.gauss(0, 0.4)
    return [*grid.tolist(), float(movement), float(weight), float(int(gate_open)), hour, rng.choice([0, 15, 30, 45])]


def generate(rows: int, out_path: Path, seed: int = 42) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    np.random.seed(seed)

    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        header = [f"grid_{i}" for i in range(16)] + ["movement_cm", "weight_g", "gate_open", "hour", "minute", "label"]
        writer.writerow(header)
        for _ in range(rows):
            hour = rng.uniform(0, 24)
            gate_open = 23 <= hour or hour < 0.5
            label = _pick_label(hour, gate_open, rng)
            feats = _features_for(label, hour, gate_open, rng)
            writer.writerow(feats + [label])

    print(f"wrote {rows} rows to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=DATA / "synthetic.csv")
    args = parser.parse_args()
    generate(args.rows, args.out, args.seed)


if __name__ == "__main__":
    main()
