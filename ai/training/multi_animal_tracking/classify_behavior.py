"""
classify_behavior.py
Reads outputs/multi_mouse_features.csv and produces:
  - outputs/multi_mouse_behavior.csv      (per-mouse, per-frame state)
  - outputs/group_social_summary.csv      (per 10-minute window)
  - outputs/social_classifier.pkl         (trained RF social classifier)

Three classification levels:
  1. Per-mouse state  — threshold rules (matching single-mouse pipeline)
  2. Group social     — threshold rules on inter-animal distance
  3. RF social        — trained on group features (labels from level 2)

Usage:
    cd multi_animal_tracking
    python classify_behavior.py
"""

import os
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

# ── Constants ─────────────────────────────────────────────────────────────────
INPUT_FEATURES_CSV   = "outputs/multi_mouse_features.csv"
OUTPUT_BEHAVIOR_CSV  = "outputs/multi_mouse_behavior.csv"
OUTPUT_SOCIAL_CSV    = "outputs/group_social_summary.csv"
OUTPUT_CLASSIFIER    = "outputs/social_classifier.pkl"

# Per-mouse state thresholds
WAKE_THRESHOLD        = 2.5     # px/frame — above this → Wake / Active
POSSIBLE_SLEEP_MIN    = 450     # consecutive quiet frames → Possible Sleep

# Social behavior thresholds
HUDDLE_THRESHOLD      = 80.0    # px mean inter-animal dist → Huddling
EXPLORE_THRESHOLD     = 200.0   # px mean inter-animal dist → Exploration
BIN_MINUTES           = 10      # window size for social summary
FPS_EFFECTIVE         = 15.0    # effective decoded FPS (25 / FRAME_SKIP=2 ≈ 12.5, ~15)
FRAMES_PER_BIN        = int(BIN_MINUTES * 60 * FPS_EFFECTIVE)

# RF classifier
RF_N_ESTIMATORS       = 100
RF_RANDOM_STATE       = 42
RF_TEST_SPLIT         = 0.2

SOCIAL_FEATURES       = [
    "mean_interanimal_dist", "min_interanimal_dist",
    "spatial_spread", "cluster_count", "isolated_count", "active_mice",
]

BEHAVIOR_HEADER = ["frame", "timestamp_s", "mouse_id", "state",
                   "behavior", "social_class"]

SOCIAL_SUMMARY_HEADER = [
    "window_start", "window_end", "timestamp_start", "timestamp_end",
    "mean_active_mice", "dominant_state", "social_class",
    "mean_interanimal_dist", "cluster_count",
    "huddling_pct", "exploration_pct",
]


# ── Level 1 — per-mouse state ─────────────────────────────────────────────────
def _classify_mouse_state(vel_smooth, quiet_streak):
    if vel_smooth > WAKE_THRESHOLD:
        return "Wake / Active"
    elif quiet_streak >= POSSIBLE_SLEEP_MIN:
        return "Possible Sleep"
    else:
        return "Quiet / Rest"


# ── Level 2 — social behavior threshold ──────────────────────────────────────
def _classify_social_threshold(mean_dist):
    if np.isnan(mean_dist):
        return "Single"        # only 0–1 mice visible
    elif mean_dist < HUDDLE_THRESHOLD:
        return "Huddling"
    elif mean_dist > EXPLORE_THRESHOLD:
        return "Exploration"
    else:
        return "Normal"


# ── Level 3 — RF social classifier ───────────────────────────────────────────
def _train_social_classifier(features_df: pd.DataFrame):
    """
    Train RF on group features. Labels generated from threshold rules.
    Saves model to OUTPUT_CLASSIFIER.
    """
    # Use rows where all group features are available (≥2 mice in frame)
    grp = features_df.groupby("frame").first().reset_index()
    grp = grp.dropna(subset=SOCIAL_FEATURES + ["mean_interanimal_dist"])

    if len(grp) < 50:
        print("  Skipping RF training — insufficient multi-mouse frames "
              f"(need ≥50, got {len(grp)})")
        return None

    X = grp[SOCIAL_FEATURES].values.astype(np.float32)
    y = grp["mean_interanimal_dist"].apply(_classify_social_threshold).values

    # Drop "Single" rows from training
    mask = y != "Single"
    X, y = X[mask], y[mask]
    if len(np.unique(y)) < 2:
        print("  Skipping RF training — only one social class present")
        return None

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=RF_TEST_SPLIT,
        random_state=RF_RANDOM_STATE, stratify=y)

    clf = RandomForestClassifier(
        n_estimators=RF_N_ESTIMATORS,
        class_weight="balanced",
        random_state=RF_RANDOM_STATE,
        n_jobs=-1,
    )
    clf.fit(X_tr, y_tr)
    y_pred = clf.predict(X_te)

    print("\n  RF Social Classifier — classification report:")
    print(classification_report(y_te, y_pred, zero_division=0))

    joblib.dump(clf, OUTPUT_CLASSIFIER, compress=3)
    print(f"  Saved: {OUTPUT_CLASSIFIER}")
    return clf


# ── Group social summary (per BIN_MINUTES window) ────────────────────────────
def _build_social_summary(features_df: pd.DataFrame,
                           behavior_df: pd.DataFrame) -> pd.DataFrame:
    """One row per BIN_MINUTES window."""
    frames = sorted(features_df["frame"].unique())
    if not frames:
        return pd.DataFrame(columns=SOCIAL_SUMMARY_HEADER)

    f_min, f_max = min(frames), max(frames)
    rows = []

    win_start = f_min
    while win_start <= f_max:
        win_end  = win_start + FRAMES_PER_BIN - 1
        mask_f   = (features_df["frame"] >= win_start) & (features_df["frame"] <= win_end)
        mask_b   = (behavior_df["frame"]  >= win_start) & (behavior_df["frame"]  <= win_end)
        sub_f    = features_df[mask_f]
        sub_b    = behavior_df[mask_b]

        if sub_f.empty:
            win_start += FRAMES_PER_BIN
            continue

        ts_start = sub_f["timestamp_s"].min()
        ts_end   = sub_f["timestamp_s"].max()

        mean_dist = sub_f["mean_interanimal_dist"].mean(skipna=True)
        sc_counts = sub_b["social_class"].value_counts(normalize=True) if not sub_b.empty else {}

        dominant_state = (sub_b["state"].value_counts().idxmax()
                          if not sub_b.empty else "")
        social_class   = _classify_social_threshold(mean_dist)

        rows.append({
            "window_start":         win_start,
            "window_end":           win_end,
            "timestamp_start":      round(ts_start, 1),
            "timestamp_end":        round(ts_end, 1),
            "mean_active_mice":     round(sub_f["active_mice"].mean(), 2),
            "dominant_state":       dominant_state,
            "social_class":         social_class,
            "mean_interanimal_dist": round(mean_dist, 1) if not np.isnan(mean_dist) else "",
            "cluster_count":        round(sub_f["cluster_count"].mean(), 2),
            "huddling_pct":         round(sc_counts.get("Huddling", 0) * 100, 1),
            "exploration_pct":      round(sc_counts.get("Exploration", 0) * 100, 1),
        })
        win_start += FRAMES_PER_BIN

    return pd.DataFrame(rows, columns=SOCIAL_SUMMARY_HEADER)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.makedirs("outputs", exist_ok=True)

    if not os.path.exists(INPUT_FEATURES_CSV):
        print(f"ERROR: input not found: {INPUT_FEATURES_CSV}")
        sys.exit(1)

    print(f"Loading {INPUT_FEATURES_CSV} ...")
    df = pd.read_csv(INPUT_FEATURES_CSV)
    df["timestamp_s"] = pd.to_numeric(df["timestamp_s"], errors="coerce")
    print(f"  {len(df):,} rows,  {df['mouse_id'].nunique()} mice,  "
          f"{df['frame'].nunique():,} frames")

    # ── Level 1: per-mouse state ──────────────────────────────────────────────
    print("\nClassifying per-mouse states ...")
    df["state"] = df.apply(
        lambda r: _classify_mouse_state(
            r.get("velocity_smooth", 0) or 0,
            r.get("quiet_streak", 0) or 0),
        axis=1,
    )

    # ── Level 2: per-frame social class (threshold) ───────────────────────────
    print("Classifying social behavior (threshold rules) ...")
    df["social_class"] = df["mean_interanimal_dist"].apply(_classify_social_threshold)

    # Placeholder behavior column (social is per-frame, same for all mice)
    df["behavior"] = df["social_class"]

    # ── Level 3: train RF social classifier ──────────────────────────────────
    print("\nTraining RF social classifier ...")
    _train_social_classifier(df)

    # ── Write per-mouse behavior CSV ──────────────────────────────────────────
    behavior_df = df[["frame", "timestamp_s", "mouse_id",
                       "state", "behavior", "social_class"]].copy()
    behavior_df.to_csv(OUTPUT_BEHAVIOR_CSV, index=False)
    print(f"\nSaved: {OUTPUT_BEHAVIOR_CSV}  ({len(behavior_df):,} rows)")

    # ── Build and write group social summary ──────────────────────────────────
    print("Building group social summary ...")
    summary_df = _build_social_summary(df, behavior_df)
    summary_df.to_csv(OUTPUT_SOCIAL_CSV, index=False)
    print(f"Saved: {OUTPUT_SOCIAL_CSV}  ({len(summary_df):,} windows)")

    # ── State breakdown per mouse ─────────────────────────────────────────────
    print("\nState breakdown per mouse:")
    breakdown = (df.groupby(["mouse_id", "state"])
                   .size()
                   .unstack(fill_value=0))
    print(breakdown.to_string())


if __name__ == "__main__":
    main()
