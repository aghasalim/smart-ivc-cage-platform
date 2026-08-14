"""
train_classifier_augmented.py
Retrains the RF state classifier on augmented data from augment_data.py.

  Train : outputs/tracking_data_augmented.csv  (noise + jitter + SMOTE)
  Test  : original 20% held-out split from outputs/tracking_data.csv  (no augmentation)
  Output: outputs/state_classifier_augmented.pkl

The test set is identical to train_classifier.py's evaluation set — same 80/20
stratified split with random_state=42 — ensuring a fair apples-to-apples comparison.

Run: python train_classifier_augmented.py
Do NOT retrain the original state_classifier.pkl.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

from train_classifier import (
    TRACKING_CSV, STATE_FEATURE_NAMES,
    RANDOM_STATE, N_ESTIMATORS, CLASS_WEIGHT,
    build_features,
)
from augment_data import AUGMENTED_CSV_PATH

# ── Output path ───────────────────────────────────────────────────────────────
AUGMENTED_STATE_MODEL_PATH = "outputs/state_classifier_augmented.pkl"

# ── Baseline metrics from train_classifier.py (sklearn 1.8.0 retrain) ────────
BASELINE = {
    "Wake / Active":  {"f1": 1.000, "precision": 1.000, "recall": 1.000},
    "Quiet / Rest":   {"f1": 0.983, "precision": 0.970, "recall": 0.996},
    "Possible Sleep": {"f1": 0.750, "precision": 0.935, "recall": 0.626},
    "accuracy": 0.984,
}

_ALL_CLASSES = ["Wake / Active", "Quiet / Rest", "Possible Sleep"]


def main():
    print("=" * 64)
    print("RF Training on Augmented Data")
    print(f"  Train: {AUGMENTED_CSV_PATH}")
    print(f"  Test : {TRACKING_CSV}  (original 20% held-out split)")
    print("=" * 64)

    # ── 1. Reconstruct original test set ─────────────────────────────────────
    # Use the exact same split parameters as train_classifier.py to get the
    # identical test fold. The augmented model is never evaluated on augmented data.
    print(f"\n[1] Loading {TRACKING_CSV} and extracting test split ...")
    df_orig    = pd.read_csv(TRACKING_CSV)
    feat_orig  = build_features(df_orig)
    valid_mask = feat_orig[STATE_FEATURE_NAMES].notna().all(axis=1)
    X_orig     = feat_orig[valid_mask][STATE_FEATURE_NAMES].values.astype(np.float32)
    y_orig     = df_orig[valid_mask]["state"].values

    # Identical split to train_classifier.py — produces same test indices
    _, X_test, _, y_test = train_test_split(
        X_orig, y_orig,
        test_size=0.20,
        random_state=RANDOM_STATE,
        stratify=y_orig,
    )
    print(f"    Original valid rows : {len(X_orig):,}")
    print(f"    Test set size       : {len(X_test):,}")

    from collections import Counter
    for cls in _ALL_CLASSES:
        n = Counter(y_test).get(cls, 0)
        print(f"      {cls:<18}  {n:>6,}")

    # ── 2. Load augmented training set ────────────────────────────────────────
    print(f"\n[2] Loading augmented training set from {AUGMENTED_CSV_PATH} ...")
    aug_df  = pd.read_csv(AUGMENTED_CSV_PATH)
    X_train = aug_df[STATE_FEATURE_NAMES].values.astype(np.float32)
    y_train = aug_df["state"].values
    print(f"    Augmented train size: {len(X_train):,} rows")

    # ── 3. Train RF with identical hyperparameters ────────────────────────────
    print(f"\n[3] Training RF  (n_estimators={N_ESTIMATORS}, "
          f"class_weight='{CLASS_WEIGHT}', random_state={RANDOM_STATE}) ...")
    clf = RandomForestClassifier(
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
        class_weight=CLASS_WEIGHT,
        n_jobs=-1,
    )
    t0 = time.perf_counter()
    clf.fit(X_train, y_train)
    elapsed = time.perf_counter() - t0
    print(f"    Done in {elapsed:.1f}s  |  n_features={clf.n_features_in_}")

    # ── 4. Evaluate on original test set ─────────────────────────────────────
    print("\n[4] Evaluating on original 20% test set ...")
    y_pred  = clf.predict(X_test)
    report  = classification_report(y_test, y_pred, output_dict=True, digits=3)
    print(classification_report(y_test, y_pred, digits=3))

    # ── 5. Save augmented model ───────────────────────────────────────────────
    os.makedirs("outputs", exist_ok=True)
    joblib.dump(clf, AUGMENTED_STATE_MODEL_PATH, compress=3)
    size_mb = os.path.getsize(AUGMENTED_STATE_MODEL_PATH) / (1024 ** 2)
    print(f"[5] Saved -> {AUGMENTED_STATE_MODEL_PATH}  ({size_mb:.2f} MB)")

    # ── 6. Comparison table ───────────────────────────────────────────────────
    acc_base = BASELINE["accuracy"]
    acc_aug  = report["accuracy"]

    print("\n" + "=" * 64)
    print("COMPARISON: Original RF  vs  Augmented RF")
    print("=" * 64)
    print(f"\n  {'Class':<20} {'Orig F1':>10}  {'Aug F1':>10}  {'Delta':>10}")
    print(f"  {'-' * 54}")

    for cls in _ALL_CLASSES:
        f1_base = BASELINE[cls]["f1"]
        f1_aug  = report.get(cls, {}).get("f1-score", 0.0)
        delta   = f1_aug - f1_base
        sign    = "+" if delta >= 0 else ""
        print(f"  {cls:<20} {f1_base:>10.3f}  {f1_aug:>10.3f}  {sign}{delta:>9.3f}")

    print(f"  {'-' * 54}")
    delta_acc = acc_aug - acc_base
    sign_acc  = "+" if delta_acc >= 0 else ""
    print(f"  {'Overall accuracy':<20} {acc_base:>10.1%}  {acc_aug:>10.1%}  "
          f"{sign_acc}{delta_acc:>+9.1%}")

    # ── 7. Possible Sleep highlight ───────────────────────────────────────────
    ps_f1_base = BASELINE["Possible Sleep"]["f1"]
    ps_f1_aug  = report.get("Possible Sleep", {}).get("f1-score", 0.0)
    ps_rec_base = BASELINE["Possible Sleep"]["recall"]
    ps_rec_aug  = report.get("Possible Sleep", {}).get("recall", 0.0)
    improved    = ps_f1_aug > ps_f1_base

    print(f"\n  Possible Sleep F1 improved : {'YES' if improved else 'NO'}")
    print(f"    F1     : {ps_f1_base:.3f} -> {ps_f1_aug:.3f}  "
          f"({'+'  if ps_f1_aug >= ps_f1_base else ''}{ps_f1_aug - ps_f1_base:+.3f})")
    print(f"    Recall : {ps_rec_base:.3f} -> {ps_rec_aug:.3f}  "
          f"({'+'  if ps_rec_aug >= ps_rec_base else ''}{ps_rec_aug - ps_rec_base:+.3f})")
    print("=" * 64)


if __name__ == "__main__":
    main()
