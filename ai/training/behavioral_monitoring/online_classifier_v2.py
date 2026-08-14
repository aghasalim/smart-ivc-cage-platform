"""
online_classifier_v2.py
SGD online classifier with 13 engineered features (9 original + 4 new interaction
and polynomial terms).  Same RF teacher, window size, and SGD hyperparameters as
online_classifier.py — only the feature set changes.

Goal: push cumulative agreement above 90% by giving the linear SGD richer inputs.

New features:
    distance_x_motion_area  = distance_from_previous_px × motion_area
    cv_movement             = dist_roll_std_15 / (dist_roll_mean_15 + 1e-6)
    quiet_streak_sq         = quiet_streak ** 2
    movement_smooth_sq      = movement_smooth ** 2

Do NOT modify online_classifier.py or online_classifier_threshold.py.

Run: python online_classifier_v2.py
"""

import os
import sys
import json
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
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

# ── Import shared constants and utilities (no duplication) ────────────────────
from online_classifier import (
    TRACKING_CSV, FEATURE_COLUMNS_PATH, STATE_MODEL_PATH,
    ONLINE_WINDOW_SIZE, MIN_WINDOWS_BEFORE_EVAL, SGD_LEARNING_RATE, ALL_CLASSES,
    _REC_START_S, _frame_to_clock, _build_features,
    LEARNING_LOG_PATH    as RF_LOG_PATH,
)
from online_classifier_threshold import (
    THRESHOLD_LOG_PATH,
)

# ── V2-specific paths ─────────────────────────────────────────────────────────
SGD_V2_MODEL_PATH       = "outputs/sgd_v2_classifier.pkl"
V2_LOG_PATH             = "outputs/sgd_v2_learning_log.csv"
V2_FEATURE_COLUMNS_PATH = "outputs/feature_columns_v2.json"
COMPARISON_V2_PLOT_PATH = "outputs/plots/sgd_comparison_v2.png"

# ── V2 feature names (9 original + 4 new) ────────────────────────────────────
V2_ORIGINAL_FEATURES = [
    "distance_from_previous_px",
    "movement_smooth",
    "is_inactive_smooth_int",
    "motion_area",
    "dist_roll_mean_15",
    "dist_roll_std_15",
    "area_roll_mean_15",
    "zone_enc",
    "quiet_streak",
]
V2_NEW_FEATURES = [
    "distance_x_motion_area",   # interaction: distance × motion area
    "cv_movement",               # coefficient of variation of rolling distance
    "quiet_streak_sq",           # polynomial: quiet_streak²
    "movement_smooth_sq",        # polynomial: movement_smooth²
]
V2_ALL_FEATURES = V2_ORIGINAL_FEATURES + V2_NEW_FEATURES   # 13 total


def _build_features_v2(df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    """
    Extend the 9-feature base with 4 engineered interaction / polynomial terms.
    Rolling features in the base set are computed over the full DataFrame before
    windowing — no per-window recomputation needed.
    """
    base = _build_features(df, meta)          # 9 original features

    # Interaction: distance × motion area (large fast-moving body = clearly active)
    base["distance_x_motion_area"] = (
        base["distance_from_previous_px"] * base["motion_area"]
    )

    # Coefficient of variation: rolling std / (rolling mean + ε)
    # Captures movement consistency — sustained active vs sporadic bursts
    base["cv_movement"] = (
        base["dist_roll_std_15"] / (base["dist_roll_mean_15"] + 1e-6)
    )

    # Polynomial: quiet_streak²  — helps SGD approximate the sharp sleep threshold
    base["quiet_streak_sq"] = base["quiet_streak"] ** 2

    # Polynomial: movement_smooth²  — amplifies separation between wake and rest
    base["movement_smooth_sq"] = base["movement_smooth"] ** 2

    return base


def main():
    print("=" * 64)
    print("Online Classifier v2  |  13 engineered features")
    print("=" * 64)

    # ── 1. Load data ──────────────────────────────────────────────────────────
    print(f"\n[1] Loading {TRACKING_CSV} ...")
    df = pd.read_csv(TRACKING_CSV)
    print(f"    {len(df):,} rows")

    # ── 2. Load feature manifest + RF teacher ─────────────────────────────────
    print(f"\n[2] Loading meta + RF teacher ...")
    with open(FEATURE_COLUMNS_PATH) as f:
        meta = json.load(f)

    rf_clf = joblib.load(STATE_MODEL_PATH)
    print(f"    RF loaded  (n_features_in={rf_clf.n_features_in_})")

    # ── 3. Feature engineering — 13 features ──────────────────────────────────
    print("\n[3] Engineering 13 features ...")
    feat_df    = _build_features_v2(df, meta)
    valid_mask = feat_df[V2_ALL_FEATURES].notna().all(axis=1)
    feat_clean = feat_df[valid_mask]
    X_all      = feat_clean[V2_ALL_FEATURES].values.astype(np.float32)
    frames_all = df[valid_mask]["frame"].values

    dropped = (~valid_mask).sum()
    print(f"    {len(X_all):,} valid rows  ({dropped} dropped)  "
          f"feature shape: {X_all.shape}")
    print(f"    Features: {V2_ALL_FEATURES}")

    # ── 4. RF teacher: predict on 9 original features (unchanged) ─────────────
    # RF was trained on 9 features — extract that sub-matrix for RF inference
    print("\n[4] RF teacher predicting (using original 9 features) ...")
    X_rf   = feat_clean[V2_ORIGINAL_FEATURES].values.astype(np.float32)
    t0     = time.perf_counter()
    y_rf_all = rf_clf.predict(X_rf)
    print(f"    Done in {time.perf_counter() - t0:.1f}s")
    rf_counts = Counter(y_rf_all)
    for cls in ALL_CLASSES:
        print(f"      {cls:<18} {rf_counts.get(cls, 0) / len(y_rf_all) * 100:5.1f}%")

    # ── 5. Initialize SGD student — identical hyperparameters, 13 features ─────
    print("\n[5] Initializing SGD v2 (13 features, identical hyperparameters) ...")
    cw_values = compute_class_weight(
        class_weight="balanced",
        classes=np.array(ALL_CLASSES),
        y=y_rf_all,
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

    # ── 6. Online learning: predict-then-update ───────────────────────────────
    n_windows = len(X_all) // ONLINE_WINDOW_SIZE
    print(f"\n[6] Streaming {n_windows} windows x {ONLINE_WINDOW_SIZE} rows ...")

    log_rows         = []
    cumulative_agree = 0
    is_fitted        = False

    for w in range(n_windows):
        start    = w * ONLINE_WINDOW_SIZE
        end      = start + ONLINE_WINDOW_SIZE
        X_w      = X_all[start:end]          # 13 features for SGD
        y_rf_w   = y_rf_all[start:end]
        frame_w  = int(frames_all[start])
        clock    = _frame_to_clock(frame_w)

        rf_label = Counter(y_rf_w).most_common(1)[0][0]

        if is_fitted:
            y_sgd_w    = sgd.predict(X_w)
            probs_w    = sgd.predict_proba(X_w)
            sgd_label  = Counter(y_sgd_w).most_common(1)[0][0]
            confidence = float(np.mean(np.max(probs_w, axis=1)))
        else:
            sgd_label  = ALL_CLASSES[0]
            confidence = 0.0

        sgd.partial_fit(X_w, y_rf_w, classes=ALL_CLASSES)
        is_fitted = True

        agree = bool(sgd_label == rf_label)
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
            "agreement":                agree,
            "confidence":               round(confidence, 4),
            "cumulative_agreement_pct": round(cum_pct, 2),
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
                f"RF: {rf_label:<16} | SGD: {sgd_label:<16} | "
                f"Agreement: {str(agree):<5} | Conf: {confidence:.2f}"
                f"{drift_tag}{warmup_tag}"
            )

    # ── 7. Feature importance via coef_ magnitude ────────────────────────────
    # coef_ shape: (n_classes, n_features) — mean |coef| across classes
    coef_importance = sorted(
        zip(V2_ALL_FEATURES, np.abs(sgd.coef_).mean(axis=0)),
        key=lambda t: t[1], reverse=True,
    )

    # ── 8. Save model ─────────────────────────────────────────────────────────
    os.makedirs("outputs", exist_ok=True)
    print(f"\n[7] Saving model -> {SGD_V2_MODEL_PATH} ...")
    joblib.dump(sgd, SGD_V2_MODEL_PATH, compress=3)
    print(f"    {os.path.getsize(SGD_V2_MODEL_PATH) / 1024:.0f} KB")

    # ── 9. Save learning log ──────────────────────────────────────────────────
    log_df = pd.DataFrame(log_rows)
    log_df.to_csv(V2_LOG_PATH, index=False)
    print(f"[8] Learning log -> {V2_LOG_PATH}  ({len(log_df)} rows)")

    # ── 10. Save feature manifest ─────────────────────────────────────────────
    v2_meta = {
        "state_features":    meta["state_features"],        # original 9 for RF
        "state_features_v2": V2_ALL_FEATURES,               # 13 for SGD v2
        "v2_new_features":   V2_NEW_FEATURES,
        "behavior_features": meta["behavior_features"],
        "zone_to_int":       meta["zone_to_int"],
        "rolling_window":    meta["rolling_window"],
    }
    with open(V2_FEATURE_COLUMNS_PATH, "w") as f:
        json.dump(v2_meta, f, indent=2)
    print(f"[9] Feature manifest -> {V2_FEATURE_COLUMNS_PATH}")

    # ── 11. Comparison plot ───────────────────────────────────────────────────
    print("\n[10] Generating three-way comparison plot ...")
    rf_log  = pd.read_csv(RF_LOG_PATH)
    thr_log = pd.read_csv(THRESHOLD_LOG_PATH)
    _generate_comparison_v2(rf_log, thr_log, log_df)

    # ── 12. Report ────────────────────────────────────────────────────────────
    _print_report(rf_log, thr_log, log_df, coef_importance)


def _clock_to_dt(hms: str) -> datetime:
    h, m, s = hms.split(":")
    cs  = int(h) * 3600 + int(m) * 60 + int(s)
    rel = cs - _REC_START_S if cs >= _REC_START_S else 86400 - _REC_START_S + cs
    return datetime(2024, 1, 1) + timedelta(seconds=rel)


def _generate_comparison_v2(
    rf_log:  pd.DataFrame,
    thr_log: pd.DataFrame,
    v2_log:  pd.DataFrame,
) -> None:
    """Three-way comparison plot: RF-taught, threshold-taught, v2 with 13 features."""
    os.makedirs("outputs/plots", exist_ok=True)

    x_rf  = rf_log["timestamp_clock"].apply(_clock_to_dt)
    x_thr = thr_log["timestamp_clock"].apply(_clock_to_dt)
    x_v2  = v2_log["timestamp_clock"].apply(_clock_to_dt)

    warmup_end_x = x_rf.iloc[min(MIN_WINDOWS_BEFORE_EVAL, len(x_rf) - 1)]

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.axvspan(x_rf.iloc[0], warmup_end_x, color="gray", alpha=0.12,
               label=f"Warm-up ({MIN_WINDOWS_BEFORE_EVAL} windows)")
    ax.axhline(90, color="dimgray", linestyle="--", linewidth=1.3,
               alpha=0.7, label="90% target")

    ax.plot(x_rf,  rf_log["cumulative_agreement_pct"],
            color="steelblue",  linewidth=1.8, label="SGD v1 — RF teacher (9 features)")
    ax.plot(x_thr, thr_log["cumulative_agreement_pct"],
            color="darkorange", linewidth=1.8, label="SGD v1 — threshold teacher (9 features)")
    ax.plot(x_v2,  v2_log["cumulative_agreement_pct"],
            color="seagreen",   linewidth=2.4, label="SGD v2 — RF teacher (13 features)")

    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=9)

    ax.set_xlabel("Time of day (elapsed since recording start)", fontsize=11)
    ax.set_ylabel("Cumulative agreement with RF teacher (%)", fontsize=11)
    ax.set_ylim(0, 105)
    ax.set_xlim(x_rf.iloc[0], x_rf.iloc[-1])
    ax.set_title("Online classifier v2 — can better features reach 90%?",
                 fontsize=13, fontweight="bold", pad=12)
    ax.legend(fontsize=9, framealpha=0.85, loc="lower right")
    ax.grid(alpha=0.3, linestyle="--")

    plt.tight_layout()
    plt.savefig(COMPARISON_V2_PLOT_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    size_kb = os.path.getsize(COMPARISON_V2_PLOT_PATH) // 1024
    print(f"    {COMPARISON_V2_PLOT_PATH}  ({size_kb} KB)")


def _print_report(
    rf_log: pd.DataFrame,
    thr_log: pd.DataFrame,
    v2_log:  pd.DataFrame,
    coef_importance: list,
) -> None:
    mid = len(rf_log) // 2

    def _pct(log, idx):
        return round(log["cumulative_agreement_pct"].iloc[idx], 1)

    wu = MIN_WINDOWS_BEFORE_EVAL
    metrics = {
        "RF v1 (9 feat)":  (_pct(rf_log,  min(wu, len(rf_log)-1)),  _pct(rf_log,  mid), _pct(rf_log,  -1)),
        "Thr v1 (9 feat)": (_pct(thr_log, min(wu, len(thr_log)-1)), _pct(thr_log, mid), _pct(thr_log, -1)),
        "RF v2 (13 feat)": (_pct(v2_log,  min(wu, len(v2_log)-1)),  _pct(v2_log,  mid), _pct(v2_log,  -1)),
    }

    v2_final   = metrics["RF v2 (13 feat)"][2]
    hit_target = v2_final >= 90.0

    print("\n" + "=" * 64)
    print("REPORT — SGD v2 with 13 features")
    print("=" * 64)

    print(f"\n  {'Version':<20} {'After warm-up':>14} {'Mid-point':>11} {'Final':>7}")
    print(f"  {'-'*56}")
    for name, (wu_v, mid_v, fin_v) in metrics.items():
        print(f"  {name:<20} {wu_v:>13.1f}%  {mid_v:>9.1f}%  {fin_v:>5.1f}%")

    print(f"\n  90% target reached : {'YES' if hit_target else 'NO'}  "
          f"(v2 final = {v2_final:.1f}%)")

    v1_rf_final = metrics["RF v1 (9 feat)"][2]
    gain = round(v2_final - v1_rf_final, 1)
    print(f"  Gain vs RF v1      : {gain:+.1f} pp")

    print(f"\n  Feature importance (mean |coef| across classes):")
    print(f"  {'Feature':<30} {'Mean |coef|':>12}  Note")
    print(f"  {'-'*60}")
    new_set = set(V2_NEW_FEATURES)
    for feat, imp in coef_importance:
        tag = " *** NEW" if feat in new_set else ""
        bar = "#" * int(imp * 20 / (coef_importance[0][1] + 1e-9))
        print(f"  {feat:<30} {imp:>12.4f}  {bar}{tag}")

    # Which new feature contributed most
    new_only = [(f, i) for f, i in coef_importance if f in new_set]
    if new_only:
        top_new = new_only[0]
        print(f"\n  Top new feature    : {top_new[0]}  ({top_new[1]:.4f})")

    print("=" * 64)


if __name__ == "__main__":
    main()
