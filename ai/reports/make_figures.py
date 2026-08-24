"""Draw the figure behind the 'number I am not going to advertise' section.

The behaviour classifier scores macro-F1 0.996 on a held-out split of the same
generator that produced its training data.  This script shows why that number
says nothing about a real mouse: the generator writes its labels as disjoint
bands in a single feature, and a plain decision tree recovers them.

    python ai/reports/make_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import f1_score
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "ai" / "data" / "synthetic.csv"
OUT = ROOT / "ai" / "reports" / "figures"

REPORTED_F1 = 0.996  # random forest, ai/reports/metrics.json
DEPTHS = [2, 3, 4, 6, 8, 12, None]


def separability(out: Path) -> Path:
    """Show the generator's label bands, and how cheaply a tree recovers them."""
    frame = pd.read_csv(DATA)
    labels = frame["label"]
    features = frame.drop(columns=["label"])

    figure, (left, right) = plt.subplots(1, 2, figsize=(12.5, 4.6))

    order = frame.groupby("label")["movement_cm"].median().sort_values().index
    for row, label in enumerate(order):
        values = frame.loc[labels == label, "movement_cm"]
        low, high = values.min(), values.max()
        left.barh(row, high - low, left=low, height=0.6, color="#2166ac", alpha=0.75)
        left.plot(values.median(), row, "|", color="white", markersize=12, markeredgewidth=2)
    left.set_yticks(range(len(order)))
    left.set_yticklabels(order)
    left.set_xlabel("movement_cm  (full observed range per class)")
    left.set_title(
        "One raw feature almost separates the classes\n"
        "sleeping tops out at 0.3, exploring starts at 6.0",
        fontsize=10,
    )
    left.spines[["top", "right"]].set_visible(False)

    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.25, random_state=0, stratify=labels
    )
    scores = []
    for depth in DEPTHS:
        tree = DecisionTreeClassifier(max_depth=depth, random_state=0)
        tree.fit(x_train, y_train)
        scores.append(f1_score(y_test, tree.predict(x_test), average="macro"))

    positions = np.arange(len(DEPTHS))
    right.plot(positions, scores, "o-", color="#2166ac", lw=2, label="decision tree")
    right.axhline(
        REPORTED_F1, ls="--", color="#b2182b", lw=1.4,
        label=f"random forest, the reported {REPORTED_F1:.3f}",
    )
    right.set_xticks(positions)
    right.set_xticklabels(["2", "3", "4", "6", "8", "12", "unlimited"])
    right.set_xlabel("decision tree max depth")
    right.set_ylabel("macro-F1, held-out synthetic split")
    right.set_ylim(0.4, 1.03)
    right.set_title(
        "An unpruned tree reaches the headline number\n"
        "because the labels came from rules in the first place",
        fontsize=10,
    )
    right.legend(frameon=False, fontsize=8, loc="lower right")
    right.spines[["top", "right"]].set_visible(False)

    figure.tight_layout()
    figure.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(figure)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = separability(OUT / "synthetic-separability.png")
    print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
