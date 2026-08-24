"""Draw the figure behind the 'number I am not going to advertise' section.

The behaviour classifier scores macro-F1 0.996 on a held-out split of the same
generator that produced its training data.  This script shows why that number
says nothing about a real mouse: the generator writes its labels as disjoint
bands in a single feature, and a plain decision tree recovers them.

    python ai/reports/make_figures.py
"""

from __future__ import annotations

import json
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


def confusion(out: Path) -> Path:
    """The classifier's confusion matrix, on the synthetic split.

    Included for completeness rather than as a result. The generator wrote these
    classes as disjoint feature bands, so a near-diagonal matrix here says the
    model recovered the generator's rules -- see the separability figure for why
    that is not evidence about a real mouse.
    """
    data = json.loads((ROOT / "ai" / "reports" / "metrics.json").read_text())
    matrix = np.array(data["test_confusion_matrix"], dtype=float)
    classes = data["classes"]
    normalised = matrix / matrix.sum(axis=1, keepdims=True)

    figure, ax = plt.subplots(figsize=(7.6, 6.4))
    image = ax.imshow(normalised, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(len(classes)))
    ax.set_xticklabels(classes, rotation=35, ha="right", fontsize=9)
    ax.set_yticks(range(len(classes)))
    ax.set_yticklabels(classes, fontsize=9)
    ax.set_xlabel("predicted")
    ax.set_ylabel("true")
    for i in range(len(classes)):
        for j in range(len(classes)):
            if matrix[i, j]:
                ax.text(j, i, f"{int(matrix[i, j])}", ha="center", va="center",
                        fontsize=8,
                        color="white" if normalised[i, j] > 0.5 else "0.2")
    figure.colorbar(image, ax=ax, fraction=0.045, pad=0.03, label="row-normalised")
    ax.set_title(
        f"Test split, macro-F1 {data['test_macro_f1']:.3f}.\n"
        "Synthetic data -- this measures rule recovery, not behaviour.",
        fontsize=10,
    )
    figure.tight_layout()
    figure.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(figure)
    return out


def online_agreement(out: Path) -> Path:
    """How closely the online learner tracks the offline model over a session.

    The SGD variants update on the stream; the random forest is fixed. Cumulative
    agreement is the practical question -- whether the thing running on the Pi
    stays close to the thing that was validated offline.
    """
    logs = {
        "sgd": "sgd_learning_log.csv",
        "sgd (threshold)": "sgd_threshold_learning_log.csv",
        "sgd v2": "sgd_v2_learning_log.csv",
        "ensemble": "ensemble_learning_log.csv",
    }
    directory = ROOT / "ai" / "data" / "behavioral_monitoring" / "outputs"

    figure, ax = plt.subplots(figsize=(10, 4.6))
    for label, filename in logs.items():
        path = directory / filename
        if not path.exists():
            continue
        table = pd.read_csv(path)
        ax.plot(table.window, table.cumulative_agreement_pct, lw=1.8, label=label)
    ax.set_xlabel("window")
    ax.set_ylabel("cumulative agreement with the offline model (%)")
    ax.set_ylim(0, 102)
    ax.set_title(
        "Online learners against the fixed random forest, over one session.",
        fontsize=10,
    )
    ax.legend(frameon=False, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    figure.tight_layout()
    figure.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(figure)
    return out


def behaviour_timeline(out: Path) -> Path:
    """A real session, as the dashboard sees it.

    Dominant behaviour state per window alongside movement and thigmotaxis --
    wall-hugging, a standard anxiety proxy in rodent work. This is camera output
    from the actual rig, not the synthetic generator.
    """
    table = pd.read_csv(
        ROOT / "ai" / "data" / "behavioral_monitoring" / "outputs"
        / "behavior_summary.csv"
    )
    states = list(dict.fromkeys(table.dominant_state))
    codes = [states.index(s) for s in table.dominant_state]

    figure, (top, bottom) = plt.subplots(
        2, 1, figsize=(11.5, 5.6), sharex=True,
        gridspec_kw={"height_ratios": [1, 2]},
    )
    top.scatter(table.window_start_frame, codes, c=codes, cmap="tab10", s=12)
    top.set_yticks(range(len(states)))
    top.set_yticklabels(states, fontsize=8)
    top.set_title("dominant behaviour state per window", fontsize=10)
    top.spines[["top", "right"]].set_visible(False)

    bottom.plot(table.window_start_frame, table.total_distance_px, lw=1.2,
                color="#2166ac", label="distance moved (px)")
    twin = bottom.twinx()
    twin.plot(table.window_start_frame, table.thigmotaxis_ratio, lw=1.2,
              color="#b2182b", alpha=0.75, label="thigmotaxis ratio")
    twin.set_ylabel("thigmotaxis ratio", color="#b2182b")
    twin.tick_params(axis="y", labelcolor="#b2182b")
    bottom.set_xlabel("frame")
    bottom.set_ylabel("distance moved (px)", color="#2166ac")
    bottom.tick_params(axis="y", labelcolor="#2166ac")
    bottom.spines[["top"]].set_visible(False)
    twin.spines[["top"]].set_visible(False)

    figure.suptitle(
        f"{len(table)} windows from a real recording. Thigmotaxis is wall-hugging, "
        "a standard anxiety proxy.",
        fontsize=10, y=0.02, color="0.35",
    )
    figure.tight_layout(rect=(0, 0.05, 1, 1))
    figure.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(figure)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for path in (
        separability(OUT / "synthetic-separability.png"),
        confusion(OUT / "confusion.png"),
        online_agreement(OUT / "online-agreement.png"),
        behaviour_timeline(OUT / "behaviour-timeline.png"),
    ):
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
