"""
train_classifier.py
One-time training script — Random Forest classifiers for sleep/wake state
and stereotypy behavior, trained on the 48h threshold-labeled tracking dataset.

Run once:  python train_classifier.py
Models are saved to outputs/ and loaded by realtime_monitor.py at runtime.
Do NOT import or call this from other modules.
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

from behavior_analysis import WAKE_THRESHOLD

# ── Paths ─────────────────────────────────────────────────────────────────────
TRACKING_CSV          = "outputs/tracking_data.csv"
STATE_MODEL_PATH      = "outputs/state_classifier.pkl"
BEHAVIOR_MODEL_PATH   = "outputs/behavior_classifier.pkl"
FEATURE_COLUMNS_PATH  = "outputs/feature_columns.json"

# ── Model hyperparameters ─────────────────────────────────────────────────────
N_ESTIMATORS          = 100
RANDOM_STATE          = 42
CLASS_WEIGHT          = "balanced"

# ── Rolling window size (mirrors ACTIVITY_SMOOTH_WINDOW in behavior_analysis) ─
ROLLING_WINDOW        = 15

# ── Zone encoding — must match ZONE_NAMES_9 order in behavior_analysis.py ─────
ZONE_ORDER = [
    "TopLeft", "TopCenter", "TopRight",
    "MidLeft", "Center",    "MidRight",
    "BotLeft", "BotCenter", "BotRight",
]
ZONE_TO_INT = {z: i for i, z in enumerate(ZONE_ORDER)}

# ── Label targets ─────────────────────────────────────────────────────────────
STATE_TARGET    = "state"
BEHAVIOR_TARGET = "behavior"

# ── Feature column names (written to JSON for inference) ──────────────────────
FEATURE_NAMES = [
    "distance_from_previous_px",
    "movement_smooth",
    "is_inactive_smooth_int",
    "motion_area",
    "dist_roll_mean_15",
    "dist_roll_std_15",
    "area_roll_mean_15",
    "zone_enc",
]
# State classifier gets one extra temporal feature not needed by behavior
STATE_FEATURE_NAMES = FEATURE_NAMES + ["quiet_streak"]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer all input features from the raw tracking DataFrame."""
    out = pd.DataFrame(index=df.index)

    out["distance_from_previous_px"] = df["distance_from_previous_px"]
    out["movement_smooth"]           = df["movement_smooth"]
    out["is_inactive_smooth_int"]    = df["is_inactive_smooth"].astype(int)
    out["motion_area"]               = df["motion_area"]

    out["dist_roll_mean_15"] = (
        df["distance_from_previous_px"]
        .rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW)
        .mean()
    )
    out["dist_roll_std_15"] = (
        df["distance_from_previous_px"]
        .rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW)
        .std()
    )
    out["area_roll_mean_15"] = (
        df["motion_area"]
        .rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW)
        .mean()
    )

    out["zone_enc"] = df["zone"].map(ZONE_TO_INT)

    # quiet_streak: consecutive rows where movement_smooth < WAKE_THRESHOLD,
    # reset to 0 on any active frame. Pre-computed and saved in tracking_data.csv;
    # recompute here in case the CSV predates the column.
    if "quiet_streak" in df.columns:
        out["quiet_streak"] = df["quiet_streak"].astype(float)
    else:
        streak, streaks = 0, []
        for ms in df["movement_smooth"]:
            streak = (streak + 1) if (pd.isna(ms) or ms < WAKE_THRESHOLD) else 0
            streaks.append(float(streak))
        out["quiet_streak"] = streaks

    return out


def train_model(X_train, y_train, label: str) -> RandomForestClassifier:
    clf = RandomForestClassifier(
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
        class_weight=CLASS_WEIGHT,
        n_jobs=-1,
    )
    t0 = time.perf_counter()
    clf.fit(X_train, y_train)
    elapsed = time.perf_counter() - t0
    print(f"  [{label}] trained in {elapsed:.1f}s")
    return clf


def model_size_mb(path: str) -> float:
    return round(os.path.getsize(path) / (1024 ** 2), 2)


def main():
    print("=" * 60)
    print("Mouse Behavioral Monitoring — Classifier Training")
    print("=" * 60)

    # ── 1. Load data ──────────────────────────────────────────────────────────
    print(f"\n[1] Loading {TRACKING_CSV} ...")
    df = pd.read_csv(TRACKING_CSV)
    print(f"    Loaded {len(df):,} rows  {list(df.columns)}")

    # ── 2. Feature engineering ─────────────────────────────────────────────────
    print("\n[2] Engineering features ...")
    feat_df    = build_features(df)
    y_state    = df[STATE_TARGET]
    y_behavior = df[BEHAVIOR_TARGET]

    # Separate feature matrices — state gets quiet_streak, behavior does not
    X_state_all = feat_df[STATE_FEATURE_NAMES]
    X_beh_all   = feat_df[FEATURE_NAMES]

    # ── 3. Drop NaN rows (rolling window head + any missing zone encoding) ────
    valid_mask    = X_state_all.notna().all(axis=1)
    X_state_clean = X_state_all[valid_mask].values.astype(np.float32)
    X_beh_clean   = X_beh_all[valid_mask].values.astype(np.float32)
    y_state_c     = y_state[valid_mask].values
    y_beh_c       = y_behavior[valid_mask].values

    dropped = (~valid_mask).sum()
    print(f"    State features   : {X_state_clean.shape[1]}  ({STATE_FEATURE_NAMES})")
    print(f"    Behavior features: {X_beh_clean.shape[1]}")
    print(f"    Rows after NaN drop: {len(X_state_clean):,}  (dropped {dropped})")

    print("\n    State distribution:")
    for cls, cnt in zip(*np.unique(y_state_c, return_counts=True)):
        print(f"      {cls:<18} {cnt:>7,}  ({cnt/len(y_state_c)*100:.1f}%)")

    # ── 4. 80/20 stratified split (same indices for both feature matrices) ────
    print("\n[3] Splitting 80/20 stratified by state ...")
    (X_state_tr, X_state_te,
     X_beh_tr,   X_beh_te,
     ys_tr, ys_te,
     yb_tr, yb_te) = train_test_split(
        X_state_clean, X_beh_clean, y_state_c, y_beh_c,
        test_size=0.20,
        random_state=RANDOM_STATE,
        stratify=y_state_c,
    )
    print(f"    Train: {len(X_state_tr):,}  |  Test: {len(X_state_te):,}")

    # ── 5. Retrain state classifier only; load existing behavior model ────────
    print("\n[4] Retraining state classifier with quiet_streak feature ...")
    state_clf = train_model(X_state_tr, ys_tr, STATE_TARGET)

    print(f"    Loading existing behavior model from {BEHAVIOR_MODEL_PATH} ...")
    behavior_clf = joblib.load(BEHAVIOR_MODEL_PATH)
    print(f"    [behavior] loaded (not retrained)")

    # ── 6. Evaluate ────────────────────────────────────────────────────────────
    print("\n[5] Evaluation on held-out test set (20%)")
    print("\n--- State classifier (with quiet_streak) ---")
    ys_pred = state_clf.predict(X_state_te)
    print(classification_report(ys_te, ys_pred, digits=3))

    print("--- Behavior classifier (unchanged) ---")
    yb_pred = behavior_clf.predict(X_beh_te)
    print(classification_report(yb_te, yb_pred, digits=3))

    # ── 7. Save updated state model and feature manifest ─────────────────────
    os.makedirs("outputs", exist_ok=True)
    print("[6] Saving updated state model ...")

    joblib.dump(state_clf, STATE_MODEL_PATH, compress=3)
    print(f"    {STATE_MODEL_PATH:<40} {model_size_mb(STATE_MODEL_PATH)} MB")

    with open(FEATURE_COLUMNS_PATH, "w") as f:
        json.dump({
            "state_features":    STATE_FEATURE_NAMES,
            "behavior_features": FEATURE_NAMES,
            "zone_to_int":       ZONE_TO_INT,
            "rolling_window":    ROLLING_WINDOW,
        }, f, indent=2)
    print(f"    {FEATURE_COLUMNS_PATH}  (state_features now includes quiet_streak)")

    # ── 8. Feature importances ─────────────────────────────────────────────────
    print("\n[7] Feature importances")
    for label, clf, fnames in [
        ("State",    state_clf,    STATE_FEATURE_NAMES),
        ("Behavior", behavior_clf, FEATURE_NAMES),
    ]:
        imp = sorted(zip(fnames, clf.feature_importances_),
                     key=lambda t: t[1], reverse=True)
        print(f"\n  {label}:")
        for name, score in imp:
            bar = "#" * int(score * 40)
            print(f"    {name:<30} {score:.4f}  {bar}")

    print("\n" + "=" * 60)
    print("Training complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
