"""
online_classifier_threshold.py
Identical architecture to online_classifier.py — teacher-student SGD learning —
but using the threshold-based state labels from tracking_data.csv as ground
truth instead of RF predictions.

Purpose: fair comparison between two teachers:
    RF teacher (online_classifier.py)         → sgd_learning_log.csv
    Threshold teacher (this file)             → sgd_threshold_learning_log.csv

Everything that could affect learning is held constant:
    ONLINE_WINDOW_SIZE, MIN_WINDOWS_BEFORE_EVAL, SGD parameters,
    feature set, valid_mask, window order, class weights

Only the label source changes.

Run: python online_classifier_threshold.py
Do NOT re-run online_classifier.py — load its log from disk for comparison.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from sklearn.linear_model import SGDClassifier
from sklearn.utils.class_weight import compute_class_weight
from collections import Counter
from datetime import datetime, timedelta

# ── Import shared constants and helpers from online_classifier ─────────────────
from online_classifier import (
    TRACKING_CSV, FEATURE_COLUMNS_PATH,
    ONLINE_WINDOW_SIZE, MIN_WINDOWS_BEFORE_EVAL, SGD_LEARNING_RATE, ALL_CLASSES,
    _REC_START_S, _frame_to_clock, _build_features,
    LEARNING_LOG_PATH as RF_LOG_PATH,       # RF-taught log (do not overwrite)
)

# ── Paths specific to this run ─────────────────────────────────────────────────
SGD_THRESHOLD_MODEL_PATH = "outputs/sgd_threshold_classifier.pkl"
THRESHOLD_LOG_PATH       = "outputs/sgd_threshold_learning_log.csv"
COMPARISON_PLOT_PATH     = "outputs/plots/sgd_comparison.png"


def main():
    print("=" * 64)
    print("Online Classifier  |  Threshold Teacher -> SGD Student")
    print("=" * 64)
    print("(Comparison run — same architecture as online_classifier.py,")
    print(" threshold state column used as labels instead of RF.)\n")

    # ── 1. Load data ──────────────────────────────────────────────────────────
    print(f"[1] Loading {TRACKING_CSV} ...")
    df = pd.read_csv(TRACKING_CSV)
    print(f"    {len(df):,} rows")

    # ── 2. Load feature manifest ──────────────────────────────────────────────
    print(f"\n[2] Loading feature manifest ...")
    import json
    with open(FEATURE_COLUMNS_PATH) as f:
        meta = json.load(f)
    state_feats = meta["state_features"]

    # ── 3. Feature engineering (identical valid_mask to online_classifier.py) ──
    print("\n[3] Engineering features ...")
    feat_df    = _build_features(df, meta)
    valid_mask = feat_df[state_feats].notna().all(axis=1)
    feat_clean = feat_df[valid_mask]
    X_all      = feat_clean[state_feats].values.astype(np.float32)
    frames_all = df[valid_mask]["frame"].values

    # ── 4. Threshold teacher: use state column directly — no model inference ───
    y_true_all = df[valid_mask]["state"].values
    dropped    = (~valid_mask).sum()
    print(f"    {len(X_all):,} valid rows  ({dropped} dropped — rolling window head)")

    counts = Counter(y_true_all)
    print("\n    Threshold label distribution:")
    for cls in ALL_CLASSES:
        pct = counts.get(cls, 0) / len(y_true_all) * 100
        print(f"      {cls:<18} {pct:5.1f}%")

    # ── 5. Initialize SGD student (identical hyperparameters) ─────────────────
    print("\n[5] Initializing SGD student (identical to RF-comparison run) ...")
    cw_values = compute_class_weight(
        class_weight="balanced",
        classes=np.array(ALL_CLASSES),
        y=y_true_all,
    )
    class_weight_dict = dict(zip(ALL_CLASSES, cw_values))
    print(f"    Class weights: { {k: round(v,3) for k,v in class_weight_dict.items()} }")

    sgd = SGDClassifier(
        loss="log_loss",
        random_state=42,
        class_weight=class_weight_dict,
        learning_rate=SGD_LEARNING_RATE,
        n_jobs=-1,
    )

    # ── 6. Online learning: predict-then-update (identical protocol) ──────────
    n_windows = len(X_all) // ONLINE_WINDOW_SIZE
    print(f"\n[6] Streaming {n_windows} windows x {ONLINE_WINDOW_SIZE} rows "
          f"({ONLINE_WINDOW_SIZE / 15:.0f}s per window)  "
          f"warm-up: {MIN_WINDOWS_BEFORE_EVAL} windows ...")

    log_rows         = []
    cumulative_agree = 0
    is_fitted        = False

    for w in range(n_windows):
        start      = w * ONLINE_WINDOW_SIZE
        end        = start + ONLINE_WINDOW_SIZE
        X_w        = X_all[start:end]
        y_true_w   = y_true_all[start:end]
        frame_w    = int(frames_all[start])
        clock      = _frame_to_clock(frame_w)

        # Window-level threshold label (majority vote)
        threshold_label = Counter(y_true_w).most_common(1)[0][0]

        # Predict BEFORE updating (honest evaluation)
        if is_fitted:
            y_sgd_w    = sgd.predict(X_w)
            probs_w    = sgd.predict_proba(X_w)
            sgd_label  = Counter(y_sgd_w).most_common(1)[0][0]
            confidence = float(np.mean(np.max(probs_w, axis=1)))
        else:
            sgd_label  = ALL_CLASSES[0]
            confidence = 0.0

        # Update SGD with threshold labels
        sgd.partial_fit(X_w, y_true_w, classes=ALL_CLASSES)
        is_fitted = True

        agree = bool(sgd_label == threshold_label)

        if w >= MIN_WINDOWS_BEFORE_EVAL:
            cumulative_agree += int(agree)
        windows_post_warmup = max(1, w - MIN_WINDOWS_BEFORE_EVAL + 1)
        cum_agree_pct = (cumulative_agree / windows_post_warmup * 100
                         if w >= MIN_WINDOWS_BEFORE_EVAL else 0.0)

        log_rows.append({
            "window":                   w,
            "timestamp_clock":          clock,
            "rf_label":                 threshold_label,   # column kept identical for direct comparison
            "sgd_label":                sgd_label,
            "agreement":                agree,
            "confidence":               round(confidence, 4),
            "cumulative_agreement_pct": round(cum_agree_pct, 2),
        })

        if w % 10 == 0 or w < 3:
            warmup_tag = " [WARM-UP]" if w < MIN_WINDOWS_BEFORE_EVAL else ""
            if w >= MIN_WINDOWS_BEFORE_EVAL and w >= 10:
                recent    = log_rows[max(0, w - 9): w + 1]
                drift_pct = (1 - sum(r["agreement"] for r in recent) / len(recent)) * 100
                drift_tag = f" | Drift: {drift_pct:.0f}%"
            else:
                drift_tag = ""
            print(
                f"[{clock}] Window {w:>4} — "
                f"Threshold: {threshold_label:<16} | SGD: {sgd_label:<16} | "
                f"Agreement: {str(agree):<5} | Confidence: {confidence:.2f}"
                f"{drift_tag}{warmup_tag}"
            )

    # ── 7. Save model ─────────────────────────────────────────────────────────
    os.makedirs("outputs", exist_ok=True)
    print(f"\n[7] Saving SGD model -> {SGD_THRESHOLD_MODEL_PATH} ...")
    joblib.dump(sgd, SGD_THRESHOLD_MODEL_PATH, compress=3)
    print(f"    {os.path.getsize(SGD_THRESHOLD_MODEL_PATH) / 1024:.0f} KB")

    # ── 8. Save learning log ──────────────────────────────────────────────────
    log_df = pd.DataFrame(log_rows)
    log_df.to_csv(THRESHOLD_LOG_PATH, index=False)
    print(f"[8] Learning log -> {THRESHOLD_LOG_PATH}  ({len(log_df)} rows)")

    # ── 9. Comparison plot ────────────────────────────────────────────────────
    print("\n[9] Generating comparison plot ...")
    rf_log_df = pd.read_csv(RF_LOG_PATH)
    _generate_comparison_plot(rf_log_df, log_df)

    # ── 10. Report ────────────────────────────────────────────────────────────
    _print_report(rf_log_df, log_df)


def _clock_to_dt(hms: str) -> datetime:
    """HH:MM:SS string → datetime, handling midnight rollover relative to recording start."""
    h, m, s = hms.split(":")
    cs  = int(h) * 3600 + int(m) * 60 + int(s)
    rel = cs - _REC_START_S if cs >= _REC_START_S else 86400 - _REC_START_S + cs
    return datetime(2024, 1, 1) + timedelta(seconds=rel)


def _generate_comparison_plot(rf_log: pd.DataFrame, thr_log: pd.DataFrame) -> None:
    """Plot RF-taught vs threshold-taught cumulative agreement on the same axes."""
    os.makedirs("outputs/plots", exist_ok=True)

    x_rf  = rf_log["timestamp_clock"].apply(_clock_to_dt)
    x_thr = thr_log["timestamp_clock"].apply(_clock_to_dt)

    # Warm-up end time (use RF log as reference — both have identical timestamps)
    warmup_end_x = x_rf.iloc[min(MIN_WINDOWS_BEFORE_EVAL, len(x_rf) - 1)]

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.axvspan(x_rf.iloc[0], warmup_end_x, color="gray", alpha=0.12,
               label=f"Warm-up ({MIN_WINDOWS_BEFORE_EVAL} windows)")

    ax.plot(x_rf,  rf_log["cumulative_agreement_pct"],
            color="steelblue",  linewidth=2.0, label="SGD taught by RF")
    ax.plot(x_thr, thr_log["cumulative_agreement_pct"],
            color="darkorange", linewidth=2.0, label="SGD taught by thresholds")

    ax.axhline(90, color="dimgray", linestyle="--", linewidth=1.2,
               label="90% target", alpha=0.7)

    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=9)

    ax.set_xlabel("Time of day (elapsed since recording start)", fontsize=11)
    ax.set_ylabel("Cumulative agreement with teacher (%)", fontsize=11)
    ax.set_ylim(0, 105)
    ax.set_xlim(x_rf.iloc[0], x_rf.iloc[-1])
    ax.set_title("Online classifier — which teacher produces better learning?",
                 fontsize=13, fontweight="bold", pad=12)
    ax.legend(fontsize=10, framealpha=0.85)
    ax.grid(alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(COMPARISON_PLOT_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    size_kb = os.path.getsize(COMPARISON_PLOT_PATH) // 1024
    print(f"    {COMPARISON_PLOT_PATH}  ({size_kb} KB)")


def _print_report(rf_log: pd.DataFrame, thr_log: pd.DataFrame) -> None:
    """Print final comparison report."""
    mid = len(rf_log) // 2

    def _pct(log, idx):
        return round(log["cumulative_agreement_pct"].iloc[idx], 1)

    # Warm-up end: first window after warm-up period
    wu  = MIN_WINDOWS_BEFORE_EVAL
    rf_warmup  = _pct(rf_log,  min(wu, len(rf_log)  - 1))
    thr_warmup = _pct(thr_log, min(wu, len(thr_log) - 1))
    rf_mid     = _pct(rf_log,  mid)
    thr_mid    = _pct(thr_log, mid)
    rf_final   = _pct(rf_log,  -1)
    thr_final  = _pct(thr_log, -1)

    winner = "RF teacher" if rf_final >= thr_final else "Threshold teacher"
    margin = abs(rf_final - thr_final)

    print("\n" + "=" * 64)
    print("COMPARISON REPORT")
    print("=" * 64)

    print(f"\n  {'Checkpoint':<20} {'RF-taught':>12}  {'Thr-taught':>12}")
    print(f"  {'-'*46}")
    print(f"  {'After warm-up':<20} {rf_warmup:>11.1f}%  {thr_warmup:>11.1f}%")
    print(f"  {'Mid-point':<20} {rf_mid:>11.1f}%  {thr_mid:>11.1f}%")
    print(f"  {'Final':<20} {rf_final:>11.1f}%  {thr_final:>11.1f}%")

    print(f"\n  Winner : {winner}  (margin: {margin:.1f} pp)")
    print()

    if rf_final >= thr_final:
        print("  Why RF teacher wins:")
        print("  The RF classifier aggregates motion features across all 100 trees")
        print("  before producing a label, smoothing frame-to-frame noise.  This gives")
        print("  the SGD cleaner, more consistent targets — especially at state boundaries")
        print("  where simple thresholds flip rapidly but the RF holds its prediction.")
    else:
        print("  Why threshold teacher wins:")
        print("  Threshold labels are linear functions of movement_smooth — exactly the")
        print("  kind of signal an SGD (itself a linear model) can represent faithfully.")
        print("  The RF's non-linear boundaries may be too complex for an SGD to distil,")
        print("  so simpler labels produce higher agreement.")

    print(f"\n  Threshold-taught final agreement : {thr_final:.1f}%")
    print(f"  RF-taught final agreement        : {rf_final:.1f}%")
    print("=" * 64)


if __name__ == "__main__":
    main()
