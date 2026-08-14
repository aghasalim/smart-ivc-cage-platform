"""
augment_data.py
Data augmentation pipeline for RF behavioral classification training.

Three augmentation techniques applied in sequence:
  1. Gaussian noise injection  — simulates sensor variation in continuous features
  2. Temporal jittering        — simulates timing uncertainty in rolling features
  3. SMOTE oversampling        — addresses Possible Sleep class imbalance (3.8%)

Output: outputs/tracking_data_augmented.csv
Run:    python augment_data.py

Do NOT modify tracking_data.csv or any existing model/output files.
"""

import os
import sys
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from collections import Counter
from imblearn.over_sampling import SMOTE

from train_classifier import (
    TRACKING_CSV, STATE_FEATURE_NAMES, build_features,
)

# ── Augmentation constants ────────────────────────────────────────────────────
NOISE_STD           = 0.05    # 5% of feature std added as Gaussian noise
MAX_JITTER_FRAMES   = 3       # rolling features shifted by up to ±3 frames
SMOTE_RANDOM_STATE  = 42
AUGMENTED_CSV_PATH  = "outputs/tracking_data_augmented.csv"

# Features to apply each technique to
CONTINUOUS_FEATURES = [
    "distance_from_previous_px",
    "movement_smooth",
    "motion_area",
    "dist_roll_mean_15",
    "dist_roll_std_15",
    "area_roll_mean_15",
]
ROLLING_FEATURES = [
    "dist_roll_mean_15",
    "dist_roll_std_15",
    "area_roll_mean_15",
]

_ALL_CLASSES = ["Wake / Active", "Quiet / Rest", "Possible Sleep"]


def add_gaussian_noise(df, feature_cols, noise_std=0.05, random_state=42):
    """Add Gaussian noise scaled to each feature's std deviation.
    noise_std=0.05 means 5% of each feature's std is added as noise."""
    rng = np.random.default_rng(random_state)
    out = df.copy()
    for col in feature_cols:
        if col not in out.columns:
            continue
        scale = noise_std * float(out[col].std())
        out[col] = out[col] + rng.normal(0.0, scale, size=len(out))
    return out


def temporal_jitter(df, rolling_cols, max_shift=3, random_state=42):
    """Randomly shift rolling features by up to max_shift frames.
    rolling_cols = ['dist_roll_mean_15', 'dist_roll_std_15', 'area_roll_mean_15']

    Each row independently looks up its rolling value from a nearby row
    (i + shift, shift in [-max_shift, +max_shift]), simulating the effect of
    the mouse's action occurring a few frames earlier or later.
    """
    rng = np.random.default_rng(random_state)
    out = df.copy()
    n = len(df)
    shifts = rng.integers(-max_shift, max_shift + 1, size=n)
    shifted_idx = np.clip(np.arange(n) + shifts, 0, n - 1)
    for col in rolling_cols:
        if col not in out.columns:
            continue
        out[col] = df[col].values[shifted_idx]
    return out


def oversample_minority(X, y, strategy="minority", random_state=42):
    """Apply SMOTE to oversample Possible Sleep class.
    Install: pip install imbalanced-learn"""
    smote = SMOTE(sampling_strategy=strategy, random_state=random_state)
    X_res, y_res = smote.fit_resample(X, y)
    return X_res, y_res


def main():
    print("=" * 64)
    print("Data Augmentation Pipeline")
    print("Gaussian noise  +  temporal jitter  +  SMOTE")
    print("=" * 64)

    # ── 1. Load raw tracking data ─────────────────────────────────────────────
    print(f"\n[1] Loading {TRACKING_CSV} ...")
    df = pd.read_csv(TRACKING_CSV)
    print(f"    {len(df):,} rows  ({len(df.columns)} columns)")

    # ── 2. Build feature matrix ───────────────────────────────────────────────
    print("\n[2] Engineering features ...")
    feat_df    = build_features(df)
    valid_mask = feat_df[STATE_FEATURE_NAMES].notna().all(axis=1)
    feat_clean = feat_df[valid_mask][STATE_FEATURE_NAMES].copy()
    y_all      = df[valid_mask]["state"].values
    dropped    = (~valid_mask).sum()
    print(f"    {len(feat_clean):,} valid rows  ({dropped} dropped — rolling window head)")
    print(f"    Features: {STATE_FEATURE_NAMES}")

    counts_before = Counter(y_all)
    total_before  = len(y_all)
    print("\n    Class distribution before augmentation:")
    for cls in _ALL_CLASSES:
        n = counts_before.get(cls, 0)
        print(f"      {cls:<18}  {n:>8,}  ({n / total_before * 100:5.1f}%)")

    # ── 3. Gaussian noise injection ───────────────────────────────────────────
    print(f"\n[3] Gaussian noise injection  (noise_std={NOISE_STD}) ...")
    feat_noisy = add_gaussian_noise(
        feat_clean, CONTINUOUS_FEATURES, NOISE_STD, SMOTE_RANDOM_STATE
    )
    print(f"    Targets : {CONTINUOUS_FEATURES}")
    sample_col = "movement_smooth"
    delta = float((feat_noisy[sample_col] - feat_clean[sample_col]).abs().mean())
    print(f"    Mean |noise| for {sample_col}: {delta:.6f} px")

    # ── 4. Temporal jitter ────────────────────────────────────────────────────
    print(f"\n[4] Temporal jitter  (max_shift=±{MAX_JITTER_FRAMES} frames) ...")
    feat_jittered = temporal_jitter(
        feat_noisy, ROLLING_FEATURES, MAX_JITTER_FRAMES, SMOTE_RANDOM_STATE
    )
    print(f"    Targets : {ROLLING_FEATURES}")
    sample_roll = "dist_roll_mean_15"
    delta_roll = float((feat_jittered[sample_roll] - feat_noisy[sample_roll]).abs().mean())
    print(f"    Mean |jitter| for {sample_roll}: {delta_roll:.6f} px")

    # ── 5. SMOTE oversampling ─────────────────────────────────────────────────
    print("\n[5] SMOTE oversampling  (strategy='minority', k_neighbors=5) ...")
    X_aug = feat_jittered[STATE_FEATURE_NAMES].values.astype(np.float32)
    X_res, y_res = oversample_minority(
        X_aug, y_all, strategy="minority", random_state=SMOTE_RANDOM_STATE
    )
    counts_after = Counter(y_res)
    total_after  = len(y_res)
    print(f"    {total_before:,} rows -> {total_after:,} rows  "
          f"(+{total_after - total_before:,} synthetic samples)")

    # ── 6. Save augmented dataset ─────────────────────────────────────────────
    print(f"\n[6] Saving -> {AUGMENTED_CSV_PATH} ...")
    os.makedirs("outputs", exist_ok=True)
    aug_df = pd.DataFrame(X_res, columns=STATE_FEATURE_NAMES)
    aug_df["state"] = y_res
    aug_df.to_csv(AUGMENTED_CSV_PATH, index=False)
    size_mb = os.path.getsize(AUGMENTED_CSV_PATH) / (1024 ** 2)
    print(f"    {len(aug_df):,} rows  |  {len(aug_df.columns)} columns  |  {size_mb:.1f} MB")

    # ── 7. Report ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("AUGMENTATION REPORT")
    print("=" * 64)
    print(f"\n  Original rows  : {total_before:>9,}")
    print(f"  Augmented rows : {total_after:>9,}  (+{total_after - total_before:,} synthetic)")

    print(f"\n  {'Class':<20} {'Before':>9}  {'After':>9}  {'Delta':>9}")
    print(f"  {'-' * 52}")
    for cls in _ALL_CLASSES:
        nb = counts_before.get(cls, 0)
        na = counts_after.get(cls, 0)
        delta = na - nb
        sign  = "+" if delta >= 0 else ""
        print(f"  {cls:<20} {nb:>9,}  {na:>9,}  {sign}{delta:>8,}")

    print(f"\n  Saved: {AUGMENTED_CSV_PATH}")
    print("=" * 64)


if __name__ == "__main__":
    main()
