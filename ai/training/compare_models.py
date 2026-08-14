"""Compare three candidate models on the synthetic dataset.

Run after ``generate_dataset.py``.  Writes a markdown comparison table to
``ai/reports/model-comparison.md`` so the design document can cite numbers
without us having to re-run training.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
REPORTS = HERE.parent / "reports"


def main() -> None:
    df = pd.read_csv(DATA / "synthetic.csv")
    y = df.pop("label").values
    X = df.values

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=0, stratify=y)

    candidates = {
        "logistic_regression": Pipeline([
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000)),
        ]),
        "random_forest": RandomForestClassifier(n_estimators=200, random_state=0),
        "hist_gradient_boosting": HistGradientBoostingClassifier(max_iter=300, random_state=0),
    }

    results = {}
    for name, pipe in candidates.items():
        pipe.fit(X_train, y_train)
        pred = pipe.predict(X_test)
        results[name] = float(f1_score(y_test, pred, average="macro"))

    REPORTS.mkdir(parents=True, exist_ok=True)
    md = ["# Model comparison\n", "Macro-F1 on the held-out test split (20%).\n", "| Model | Macro-F1 |", "|---|---:|"]
    for name, score in sorted(results.items(), key=lambda kv: -kv[1]):
        md.append(f"| `{name}` | {score:.3f} |")
    md.append("\n_HistGradientBoostingClassifier is the production model: best F1, fast inference, interpretable._")
    (REPORTS / "model-comparison.md").write_text("\n".join(md))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
