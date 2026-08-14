"""
ensemble_classifier.py
Production deployment architecture: weighted probability ensemble combining
the RF accuracy anchor (98.4%) with the SGD v2 adaptive learner (84.0%).

Architecture:
    RF classifier      (weight = 0.7)   — accuracy anchor, stable, high precision
    SGD v2 classifier  (weight = 0.3)   — adaptive, 13 engineered features
         ↓
    ensemble_proba = 0.7 × rf_proba + 0.3 × sgd_proba  (per-frame, then window vote)
         ↓
    Final prediction = argmax(ensemble_proba) → majority-voted window label

Comparison evaluated against RF ground truth (RF treats its own majority vote as
the reference label, same protocol as online_classifier.py).

Run: python ensemble_classifier.py
Do NOT retrain any model.
"""

import os
import sys
import json
import time
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import Counter
from datetime import datetime, timedelta

# ── Import shared utilities — no duplication ──────────────────────────────────
from online_classifier import (
    TRACKING_CSV, FEATURE_COLUMNS_PATH,
    STATE_MODEL_PATH        as RF_MODEL_PATH,
    ONLINE_WINDOW_SIZE, MIN_WINDOWS_BEFORE_EVAL, ALL_CLASSES,
    _REC_START_S, _frame_to_clock, _build_features,
    LEARNING_LOG_PATH       as RF_LOG_PATH,
)
from online_classifier_threshold import THRESHOLD_LOG_PATH
from online_classifier_v2 import (
    SGD_V2_MODEL_PATH,
    V2_LOG_PATH,
    V2_ALL_FEATURES, V2_ORIGINAL_FEATURES,
    _build_features_v2,
    _clock_to_dt,
)

# ── Ensemble constants ────────────────────────────────────────────────────────
RF_WEIGHT       = 0.7
SGD_WEIGHT      = 0.3
ENSEMBLE_WINDOW = ONLINE_WINDOW_SIZE    # 450 frames — consistent with all other runs

# ── Ensemble output paths ─────────────────────────────────────────────────────
ENSEMBLE_LOG_PATH         = "outputs/ensemble_learning_log.csv"
ENSEMBLE_CONFIG_PATH      = "outputs/ensemble_config.json"
COMPARISON_FINAL_PLOT     = "outputs/plots/sgd_comparison_final.png"


def main():
    print("=" * 64)
    print("Ensemble Classifier  |  0.7 × RF  +  0.3 × SGD v2")
    print("=" * 64)

    # ── 1. Load models ────────────────────────────────────────────────────────
    print(f"\n[1] Loading models ...")
    rf_clf   = joblib.load(RF_MODEL_PATH)
    sgd_v2   = joblib.load(SGD_V2_MODEL_PATH)
    print(f"    RF    n_features={rf_clf.n_features_in_}   "
          f"classes={list(rf_clf.classes_)}")
    print(f"    SGD v2 n_features={sgd_v2.n_features_in_}  "
          f"classes={list(sgd_v2.classes_)}")

    # ── 2. Precompute class-alignment index maps ───────────────────────────────
    # Both classifiers may store classes_ in different orders (e.g. alphabetical).
    # Map each classifier's column indices to the canonical ALL_CLASSES order so
    # probability vectors can be summed directly.
    rf_classes  = list(rf_clf.classes_)
    sgd_classes = list(sgd_v2.classes_)
    rf_class_map  = [rf_classes.index(c)  for c in ALL_CLASSES]
    sgd_class_map = [sgd_classes.index(c) for c in ALL_CLASSES]
    print(f"\n    Class alignment (canonical: {ALL_CLASSES})")
    print(f"    RF  column order : {rf_classes} -> idx {rf_class_map}")
    print(f"    SGD column order : {sgd_classes} -> idx {sgd_class_map}")

    # ── 3. Feature engineering ────────────────────────────────────────────────
    print(f"\n[2] Loading {TRACKING_CSV} and engineering features ...")
    df = pd.read_csv(TRACKING_CSV)

    with open(FEATURE_COLUMNS_PATH) as f:
        meta = json.load(f)

    feat_v2    = _build_features_v2(df, meta)
    valid_mask = feat_v2[V2_ALL_FEATURES].notna().all(axis=1)
    feat_clean = feat_v2[valid_mask]

    X_rf   = feat_clean[V2_ORIGINAL_FEATURES].values.astype(np.float32)  # (N, 9)  for RF
    X_v2   = feat_clean[V2_ALL_FEATURES].values.astype(np.float32)       # (N, 13) for SGD v2
    frames = df[valid_mask]["frame"].values
    print(f"    {len(X_rf):,} valid rows   RF shape: {X_rf.shape}   SGD v2 shape: {X_v2.shape}")

    # ── 4. Ensemble streaming ─────────────────────────────────────────────────
    n_windows = len(X_rf) // ENSEMBLE_WINDOW
    print(f"\n[3] Streaming {n_windows} windows × {ENSEMBLE_WINDOW} frames  "
          f"(RF_WEIGHT={RF_WEIGHT}  SGD_WEIGHT={SGD_WEIGHT}) ...")

    log_rows         = []
    cumulative_agree = 0
    t0               = time.perf_counter()

    for w in range(n_windows):
        s = w * ENSEMBLE_WINDOW
        e = s + ENSEMBLE_WINDOW

        X_rf_w  = X_rf[s:e]
        X_v2_w  = X_v2[s:e]
        frame_w = int(frames[s])
        clock   = _frame_to_clock(frame_w)

        # Per-frame probability vectors ─────────────────────────────────────────
        # RF:   shape (window, 3) in RF's class order → reorder to canonical
        rf_raw   = rf_clf.predict_proba(X_rf_w)
        rf_proba = rf_raw[:, rf_class_map]          # (window, 3) canonical order

        # SGD v2: shape (window, 3) → reorder
        v2_raw   = sgd_v2.predict_proba(X_v2_w)
        v2_proba = v2_raw[:, sgd_class_map]         # (window, 3) canonical order

        # Weighted ensemble probabilities
        ens_proba = RF_WEIGHT * rf_proba + SGD_WEIGHT * v2_proba  # (window, 3)

        # Per-frame argmax labels
        rf_labels  = [ALL_CLASSES[i] for i in np.argmax(rf_proba,  axis=1)]
        sgd_labels = [ALL_CLASSES[i] for i in np.argmax(v2_proba,  axis=1)]
        ens_labels = [ALL_CLASSES[i] for i in np.argmax(ens_proba, axis=1)]

        # Window-level majority vote
        rf_label  = Counter(rf_labels).most_common(1)[0][0]
        sgd_label = Counter(sgd_labels).most_common(1)[0][0]
        ens_label = Counter(ens_labels).most_common(1)[0][0]

        # Ensemble confidence: mean max probability across window frames
        confidence = float(np.mean(np.max(ens_proba, axis=1)))

        rf_sgd_agree = bool(rf_label == sgd_label)
        agree        = bool(ens_label == rf_label)

        if w >= MIN_WINDOWS_BEFORE_EVAL:
            cumulative_agree += int(agree)
        windows_post = max(1, w - MIN_WINDOWS_BEFORE_EVAL + 1)
        cum_pct = (cumulative_agree / windows_post * 100
                   if w >= MIN_WINDOWS_BEFORE_EVAL else 0.0)

        log_rows.append({
            "window":                   w,
            "timestamp_clock":          clock,
            "rf_label":                 rf_label,
            "sgd_label":                sgd_label,
            "ensemble_label":           ens_label,
            "rf_sgd_agree":             rf_sgd_agree,
            "ensemble_confidence":      round(confidence, 4),
            "cumulative_agreement_pct": round(cum_pct, 2),
        })

        if w % 10 == 0 or w < 3:
            tag = " [WARM-UP]" if w < MIN_WINDOWS_BEFORE_EVAL else ""
            print(
                f"[{clock}] Window {w:>4} — "
                f"RF: {rf_label:<16} | SGD: {sgd_label:<16} | "
                f"Ens: {ens_label:<16} | Conf: {confidence:.2f}{tag}"
            )

    elapsed = time.perf_counter() - t0
    em, es  = divmod(int(elapsed), 60)
    print(f"\n    Done in {em}m{es:02d}s")

    # ── 5. Save ensemble log ──────────────────────────────────────────────────
    log_df = pd.DataFrame(log_rows)
    os.makedirs("outputs", exist_ok=True)
    log_df.to_csv(ENSEMBLE_LOG_PATH, index=False)
    print(f"[4] Ensemble log -> {ENSEMBLE_LOG_PATH}  ({len(log_df)} rows)")

    # ── 6. Save ensemble config ───────────────────────────────────────────────
    config = {
        "rf_weight":    RF_WEIGHT,
        "sgd_weight":   SGD_WEIGHT,
        "rf_features":  len(V2_ORIGINAL_FEATURES),
        "sgd_features": len(V2_ALL_FEATURES),
        "architecture": "weighted_probability_vote",
        "rf_model":     RF_MODEL_PATH,
        "sgd_model":    SGD_V2_MODEL_PATH,
    }
    with open(ENSEMBLE_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print(f"[5] Ensemble config -> {ENSEMBLE_CONFIG_PATH}")

    # ── 7. Final comparison plot ──────────────────────────────────────────────
    print("\n[6] Generating final comparison plot ...")
    rf_log  = pd.read_csv(RF_LOG_PATH)
    thr_log = pd.read_csv(THRESHOLD_LOG_PATH)
    v2_log  = pd.read_csv(V2_LOG_PATH)
    _generate_comparison_final(rf_log, thr_log, v2_log, log_df)

    # ── 8. Report ─────────────────────────────────────────────────────────────
    _print_report(log_df)


def _generate_comparison_final(
    rf_log:  pd.DataFrame,
    thr_log: pd.DataFrame,
    v2_log:  pd.DataFrame,
    ens_log: pd.DataFrame,
) -> None:
    """Four-line comparison: SGD v1 RF | SGD v1 Thr | SGD v2 | Ensemble."""
    os.makedirs("outputs/plots", exist_ok=True)

    x_rf  = rf_log["timestamp_clock"].apply(_clock_to_dt)
    x_thr = thr_log["timestamp_clock"].apply(_clock_to_dt)
    x_v2  = v2_log["timestamp_clock"].apply(_clock_to_dt)
    x_ens = ens_log["timestamp_clock"].apply(_clock_to_dt)

    warmup_end_x = x_rf.iloc[min(MIN_WINDOWS_BEFORE_EVAL, len(x_rf) - 1)]

    # Final agreement for legend labels
    rf_fin  = rf_log["cumulative_agreement_pct"].iloc[-1]
    thr_fin = thr_log["cumulative_agreement_pct"].iloc[-1]
    v2_fin  = v2_log["cumulative_agreement_pct"].iloc[-1]
    ens_fin = ens_log["cumulative_agreement_pct"].iloc[-1]

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.axvspan(x_rf.iloc[0], warmup_end_x, color="gray", alpha=0.10,
               label=f"Warm-up ({MIN_WINDOWS_BEFORE_EVAL} windows)")
    ax.axhline(90, color="dimgray", linestyle="--", linewidth=1.3,
               alpha=0.7, label="90% target")

    ax.plot(x_rf,  rf_log["cumulative_agreement_pct"],
            color="steelblue",   linewidth=1.6,
            label=f"SGD v1 RF teacher ({rf_fin:.1f}%)")
    ax.plot(x_thr, thr_log["cumulative_agreement_pct"],
            color="darkorange",  linewidth=1.6,
            label=f"SGD v1 threshold teacher ({thr_fin:.1f}%)")
    ax.plot(x_v2,  v2_log["cumulative_agreement_pct"],
            color="seagreen",    linewidth=1.8,
            label=f"SGD v2 — 13 features ({v2_fin:.1f}%)")
    ax.plot(x_ens, ens_log["cumulative_agreement_pct"],
            color="crimson",     linewidth=2.4,
            label=f"Ensemble 0.7 RF + 0.3 SGD v2 ({ens_fin:.1f}%)")

    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=9)

    ax.set_xlabel("Time of day (elapsed since recording start)", fontsize=11)
    ax.set_ylabel("Cumulative agreement with RF teacher (%)", fontsize=11)
    ax.set_ylim(0, 105)
    ax.set_xlim(x_rf.iloc[0], x_rf.iloc[-1])
    ax.set_title("Production ensemble — accuracy anchor + adaptive online learning",
                 fontsize=13, fontweight="bold", pad=12)
    ax.legend(fontsize=9, framealpha=0.88, loc="lower right")
    ax.grid(alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(COMPARISON_FINAL_PLOT, dpi=150, bbox_inches="tight")
    plt.close()
    size_kb = os.path.getsize(COMPARISON_FINAL_PLOT) // 1024
    print(f"    {COMPARISON_FINAL_PLOT}  ({size_kb} KB)")


def _print_report(log_df: pd.DataFrame) -> None:
    """Detailed ensemble performance report."""
    final_agree = log_df["cumulative_agreement_pct"].iloc[-1]
    hit_90      = final_agree >= 90.0

    # RF vs SGD v2 disagreement analysis
    n_total      = len(log_df)
    n_disagree   = (~log_df["rf_sgd_agree"]).sum()
    n_agree      = log_df["rf_sgd_agree"].sum()
    pct_disagree = n_disagree / n_total * 100

    # When RF and SGD agree: what does ensemble do?
    agree_subset = log_df[log_df["rf_sgd_agree"]]
    ens_follows_when_agree = (
        (agree_subset["ensemble_label"] == agree_subset["rf_label"]).mean() * 100
        if len(agree_subset) > 0 else 0
    )

    # When RF and SGD disagree: does ensemble follow RF or SGD?
    disagree_subset = log_df[~log_df["rf_sgd_agree"]]
    ens_follows_rf_when_disagree = (
        (disagree_subset["ensemble_label"] == disagree_subset["rf_label"]).mean() * 100
        if len(disagree_subset) > 0 else 0
    )
    ens_follows_sgd_when_disagree = 100 - ens_follows_rf_when_disagree

    print("\n" + "=" * 64)
    print("ENSEMBLE REPORT")
    print("=" * 64)
    print(f"\n  Final cumulative agreement vs RF : {final_agree:.1f}%")
    print(f"  90% target reached              : {'YES' if hit_90 else 'NO'}")
    print(f"\n  RF vs SGD v2 disagreement:")
    print(f"    Windows where RF and SGD agree    : {n_agree:>5}  ({100-pct_disagree:5.1f}%)")
    print(f"    Windows where RF and SGD disagree : {n_disagree:>5}  ({pct_disagree:5.1f}%)"
          " <- ensemble adds value here")
    print(f"\n  Ensemble behaviour:")
    print(f"    When RF+SGD AGREE   → ensemble follows: {ens_follows_when_agree:.1f}% of time")
    print(f"    When RF+SGD DISAGREE → ensemble follows RF  : "
          f"{ens_follows_rf_when_disagree:.1f}%")
    print(f"    When RF+SGD DISAGREE → ensemble follows SGD : "
          f"{ens_follows_sgd_when_disagree:.1f}%")
    print(f"    (Expected RF dominance: {RF_WEIGHT:.0%} weight → ensemble biased toward RF)")
    print(f"\n  Mean ensemble confidence : "
          f"{log_df['ensemble_confidence'].mean():.4f}")
    print("=" * 64)


if __name__ == "__main__":
    main()
