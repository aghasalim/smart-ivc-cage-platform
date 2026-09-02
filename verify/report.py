"""Recompute the classification report from the confusion matrix in Python.

Same check as verify/report.c and verify/report.sql: derives precision, recall,
F1, support and macro-F1 from the raw 7x7 confusion matrix in
ai/reports/metrics.json and compares against the published report.

Shares no code with the training scripts.

Run: python3 verify/report.py <repo root>
"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
d = json.loads((root / "ai" / "reports" / "metrics.json").read_text())

classes = d["classes"]
cm = d["test_confusion_matrix"]
report = d["test_classification_report"]
published_f1 = d["test_macro_f1"]
n = len(classes)
bad = 0
TOL = 5e-4

def fail(msg):
    global bad
    print(f"  FAIL: {msg}")
    bad += 1

f1s = []
for i in range(n):
    tp = cm[i][i]
    fp = sum(cm[j][i] for j in range(n) if j != i)
    fn = sum(cm[i][j] for j in range(n) if j != i)
    support = sum(cm[i])
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    f1s.append(f1)

    name = classes[i]
    pub = report.get(name, {})
    for metric, got in [("precision", precision), ("recall", recall), ("f1-score", f1)]:
        pub_val = pub.get(metric)
        if pub_val is not None and abs(got - pub_val) > TOL:
            fail(f"{name} {metric}: published {pub_val:.4f}, recomputed {got:.4f}")

macro_f1 = sum(f1s) / n
if abs(macro_f1 - published_f1) > TOL:
    fail(f"macro-F1: published {published_f1:.6f}, recomputed {macro_f1:.6f}")

if bad:
    print(f"Python: {bad} problem(s)")
    sys.exit(1)
print(f"Python: {n} classes, macro-F1 {macro_f1:.4f} reproduced from confusion matrix")
