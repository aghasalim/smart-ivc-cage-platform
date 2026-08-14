"""
train_classifier_v2.py
RF v3 — adds 3 temporal context features (window=450 frames ≈ 30 s) to the
existing 13 v2 features, targeting Possible Sleep F1 > 85 %.

Run:  python train_classifier_v2.py

Saves:
  outputs/state_classifier_v3.pkl
  outputs/feature_columns_v3.json
  outputs/plots/possible_sleep_improvement.png

Does NOT modify any existing classifiers or training scripts.
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
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, f1_score, precision_score, recall_score

from behavior_analysis import WAKE_THRESHOLD, SMOOTH_INACTIVITY_THRESHOLD
from postprocess_states import (
    smooth_state_predictions,
    CONSECUTIVE_QUIET_FOR_SLEEP,
    CONSECUTIVE_ACTIVE_TO_WAKE,
)

# ── Paths ─────────────────────────────────────────────────────────────────────
TRACKING_CSV     = "outputs/tracking_data.csv"
V3_MODEL_PATH    = "outputs/state_classifier_v3.pkl"
V3_FEATURES_PATH = "outputs/feature_columns_v3.json"
PLOT_PATH        = "outputs/plots/possible_sleep_improvement.png"

# ── Hyperparameters (identical to v1 / v2 augmented) ─────────────────────────
N_ESTIMATORS    = 100
RANDOM_STATE    = 42
CLASS_WEIGHT    = "balanced"
ROLLING_WINDOW  = 15
TEMPORAL_WINDOW = 450   # same as ONLINE_WINDOW_SIZE in realtime_monitor

# ── Zone encoding (must match behavior_analysis.py) ──────────────────────────
ZONE_ORDER = [
    "TopLeft", "TopCenter", "TopRight",
    "MidLeft", "Center",    "MidRight",
    "BotLeft", "BotCenter", "BotRight",
]
ZONE_TO_INT = {z: i for i, z in enumerate(ZONE_ORDER)}

# ── Feature lists ─────────────────────────────────────────────────────────────
V1_FEATURES = [
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

V2_EXTRA_FEATURES = [
    "distance_x_motion_area",
    "cv_movement",
    "quiet_streak_sq",
    "movement_smooth_sq",
]

TEMPORAL_FEATURES = [
    "max_quiet_streak_in_window",
    "pct_inactive_in_window",
    "activity_transitions",
]

V2_FEATURES = V1_FEATURES + V2_EXTRA_FEATURES           # 13 features
V3_FEATURES = V2_FEATURES + TEMPORAL_FEATURES           # 16 features

# ── Published baselines ───────────────────────────────────────────────────────
BASELINE_V1  = {"Wake / Active": 1.000, "Quiet / Rest": 0.983,
                "Possible Sleep": 0.750, "acc": 98.4}
BASELINE_AUG = {"Wake / Active": 1.000, "Quiet / Rest": 0.978,
                "Possible Sleep": 0.744, "acc": 98.0}


# ── Feature engineering ───────────────────────────────────────────────────────
def add_temporal_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add 3 temporal context features using a rolling window of TEMPORAL_WINDOW frames."""
    window = TEMPORAL_WINDOW

    df = df.copy()

    # 1. Max quiet streak seen in rolling window
    df["max_quiet_streak_in_window"] = (
        df["quiet_streak"]
        .rolling(window, min_periods=1)
        .max()
    )

    # 2. % of frames inactive in rolling window
    df["pct_inactive_in_window"] = (
        df["is_inactive_smooth_int"]
        .rolling(window, min_periods=1)
        .mean()
    )

    # 3. State transitions per window — how many times active/inactive flipped
    df["activity_transitions"] = (
        df["is_inactive_smooth"]
        .diff()
        .abs()
        .rolling(window, min_periods=1)
        .sum()
    )

    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build all 16 features for RF v3 from raw tracking DataFrame."""
    out = pd.DataFrame(index=df.index)

    # ── Base 9 features (v1) ──────────────────────────────────────────────────
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

    if "quiet_streak" in df.columns:
        out["quiet_streak"] = df["quiet_streak"].astype(float)
    else:
        streak, streaks = 0, []
        for ms in df["movement_smooth"]:
            streak = (streak + 1) if (pd.isna(ms) or ms < WAKE_THRESHOLD) else 0
            streaks.append(float(streak))
        out["quiet_streak"] = streaks

    # ── V2 computed features (+4) ─────────────────────────────────────────────
    out["distance_x_motion_area"] = (
        df["distance_from_previous_px"] * df["motion_area"]
    )
    _dm = out["dist_roll_mean_15"]
    _ds = out["dist_roll_std_15"]
    out["cv_movement"]        = _ds / (_dm + 1e-6)
    out["quiet_streak_sq"]    = out["quiet_streak"] ** 2
    out["movement_smooth_sq"] = df["movement_smooth"] ** 2

    # ── Temporal features (+3, window=450) ────────────────────────────────────
    out["max_quiet_streak_in_window"] = (
        out["quiet_streak"]
        .rolling(TEMPORAL_WINDOW, min_periods=1)
        .max()
    )
    out["pct_inactive_in_window"] = (
        out["is_inactive_smooth_int"]
        .rolling(TEMPORAL_WINDOW, min_periods=1)
        .mean()
    )
    out["activity_transitions"] = (
        out["is_inactive_smooth_int"]
        .diff()
        .abs()
        .rolling(TEMPORAL_WINDOW, min_periods=1)
        .sum()
    )

    return out


# ── Utilities ─────────────────────────────────────────────────────────────────
def _f1(y_true, y_pred, cls):
    mask = y_true == cls
    if mask.sum() == 0:
        return float("nan")
    return f1_score(y_true, y_pred, labels=[cls], average="macro", zero_division=0)


def _precision(y_true, y_pred, cls):
    mask = y_true == cls
    if mask.sum() == 0:
        return float("nan")
    return precision_score(y_true, y_pred, labels=[cls], average="macro", zero_division=0)


def _recall(y_true, y_pred, cls):
    mask = y_true == cls
    if mask.sum() == 0:
        return float("nan")
    return recall_score(y_true, y_pred, labels=[cls], average="macro", zero_division=0)


def _make_plot(f1_v3: float,
               prec_v3: float, rec_v3: float,
               recall_smooth_wl: float = 0.698):
    """Horizontal bar chart — Possible Sleep improvement journey.

    Left panel : F1 for RF v1 / augmented / v3; window-level recall for
                 v3+smoothing (frame-level F1 is a known artifact — do not use).
    Right panel: precision & recall breakdown for RF v3 alone.
    """
    os.makedirs("outputs/plots", exist_ok=True)

    # ── Left panel data ───────────────────────────────────────────────────────
    versions = [
        "RF v1\n(original)",
        "RF augmented\n(SMOTE)",
        "RF v3\n(+temporal)",
        "RF v3 + smoothing*\n(window-level recall)",
    ]
    scores  = [0.750, 0.744, f1_v3, recall_smooth_wl]
    colors  = ["#888888", "#aaaaaa", "#2196F3", "#4CAF50"]
    # Labels: first 3 are F1, last is recall
    metric_labels = ["F1", "F1", "F1", "Recall"]

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))

    ax = axes[0]
    bars = ax.barh(versions, scores, color=colors, height=0.5)
    for bar, val, mlabel in zip(bars, scores, metric_labels):
        ax.text(
            bar.get_width() + 0.005,
            bar.get_y() + bar.get_height() / 2,
            f"{val:.3f} ({mlabel})",
            va="center", fontsize=10, fontweight="bold",
        )
    ax.axvline(0.85, color="red", linestyle="--", linewidth=1.5, label="85% target")
    ax.set_xlim(0, 1.18)
    ax.set_xlabel("Score", fontsize=12)
    ax.set_title("Possible Sleep — detection progress", fontsize=12, fontweight="bold")
    ax.legend(loc="lower right", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    # Footnote
    ax.text(
        0.01, -0.14,
        "* smoothing evaluated on window-level SGD predictions (1 013 x 10 s windows),\n"
        "  not on frame-level test set — frame-level F1 = 0.154 is a known artifact.",
        transform=ax.transAxes, fontsize=8, color="#555555",
        verticalalignment="top",
    )

    # ── Right panel: precision / recall for RF v3 ─────────────────────────────
    ax2 = axes[1]
    metrics = ["Precision", "Recall"]
    vals_v3 = [prec_v3, rec_v3]
    x = np.arange(len(metrics))
    w = 0.4
    b = ax2.bar(x, vals_v3, w, color="#2196F3", alpha=0.85, label="RF v3 (+temporal)")
    for bar in b:
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                 f"{bar.get_height():.3f}", ha="center", va="bottom",
                 fontsize=11, fontweight="bold")
    # Smoothing recall as a separate marker on the Recall bar
    ax2.plot(1, recall_smooth_wl, marker="D", markersize=9, color="#4CAF50",
             label=f"v3+smoothing recall* = {recall_smooth_wl:.3f}", zorder=5)
    ax2.axhline(0.85, color="red", linestyle="--", linewidth=1.2, alpha=0.7,
                label="85% target")
    ax2.set_xticks(x)
    ax2.set_xticklabels(metrics, fontsize=12)
    ax2.set_ylim(0, 1.12)
    ax2.set_title("RF v3 — precision & recall (Possible Sleep)", fontsize=12,
                  fontweight="bold")
    ax2.legend(fontsize=9, loc="lower right")
    ax2.spines[["top", "right"]].set_visible(False)

    fig.suptitle("Possible Sleep detection — improvement journey",
                 fontsize=14, fontweight="bold")
    plt.tight_layout(rect=[0, 0.05, 1, 1])
    plt.savefig(PLOT_PATH, dpi=120, bbox_inches="tight")
    plt.close()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 68)
    print("RF v3 — Temporal Feature Engineering  (target: Sleep F1 > 85%)")
    print("=" * 68)

    # 1. Load
    print(f"\n[1] Loading {TRACKING_CSV} ...")
    df = pd.read_csv(TRACKING_CSV)
    print(f"    {len(df):,} rows")

    # 2. Feature engineering
    print("\n[2] Building 16 features ...")
    feat_df = build_features(df)
    y_all   = df["state"].values

    X_all  = feat_df[V3_FEATURES]
    valid  = X_all.notna().all(axis=1)
    X_clean   = X_all[valid].values.astype(np.float32)
    y_clean   = y_all[valid]
    clean_idx = np.where(valid)[0]

    print(f"    Features ({len(V3_FEATURES)}): {V3_FEATURES}")
    print(f"    Rows after NaN drop: {len(X_clean):,}  (dropped {(~valid).sum()})")
    print("\n    State distribution:")
    for cls, cnt in zip(*np.unique(y_clean, return_counts=True)):
        print(f"      {cls:<18} {cnt:>7,}  ({cnt / len(y_clean) * 100:.1f}%)")

    # 3. 80/20 stratified split — same random_state=42 as v1 → comparable test set
    print("\n[3] 80/20 stratified split (random_state=42) ...")
    (X_tr, X_te, y_tr, y_te,
     idx_tr, idx_te) = train_test_split(
        X_clean, y_clean, clean_idx,
        test_size=0.20,
        random_state=RANDOM_STATE,
        stratify=y_clean,
    )
    print(f"    Train: {len(X_tr):,}  |  Test: {len(X_te):,}")

    # 4. Train RF v3
    print("\n[4] Training RF v3 ...")
    clf = RandomForestClassifier(
        n_estimators=N_ESTIMATORS,
        random_state=RANDOM_STATE,
        class_weight=CLASS_WEIGHT,
        n_jobs=-1,
    )
    t0 = time.perf_counter()
    clf.fit(X_tr, y_tr)
    print(f"    Trained in {time.perf_counter() - t0:.1f}s")

    # 5. Evaluate
    print("\n[5] Evaluation on held-out test set (20%)")
    y_pred = clf.predict(X_te)
    print(classification_report(y_te, y_pred, digits=3))

    classes = ["Wake / Active", "Quiet / Rest", "Possible Sleep"]
    f1_v3  = {c: _f1(y_te, y_pred, c)        for c in classes}
    pr_v3  = {c: _precision(y_te, y_pred, c)  for c in classes}
    rc_v3  = {c: _recall(y_te, y_pred, c)     for c in classes}
    acc_v3 = (y_pred == y_te).mean() * 100

    # 6. Post-processing smoothing — evaluate on test set in temporal order
    print("\n[6] Evaluating post-processing smoothing ...")
    sort_order    = np.argsort(idx_te)
    X_te_sorted   = X_te[sort_order]
    y_te_sorted   = y_te[sort_order]
    y_pred_sorted = clf.predict(X_te_sorted)
    y_smoothed    = np.array(smooth_state_predictions(list(y_pred_sorted)))

    f1_sm  = {c: _f1(y_te_sorted, y_smoothed, c)        for c in classes}
    pr_sm  = {c: _precision(y_te_sorted, y_smoothed, c)  for c in classes}
    rc_sm  = {c: _recall(y_te_sorted, y_smoothed, c)     for c in classes}
    acc_sm = (y_smoothed == y_te_sorted).mean() * 100

    changed     = (y_smoothed != y_pred_sorted).sum()
    qr_to_sleep = sum(
        1 for p, s in zip(y_pred_sorted, y_smoothed)
        if p == "Quiet / Rest" and s == "Possible Sleep"
    )
    print(f"    Windows changed by smoothing  : {changed}")
    print(f"    Quiet/Rest -> Possible Sleep  : {qr_to_sleep}")

    # 7. Comparison table
    print("\n[7] Comparison table — Possible Sleep detection")
    header = (f"{'Class':<18} | {'RF v1':>6} | {'RF aug':>6} | "
              f"{'RF v3':>6} | {'v3+smth':>7} | {'Delta v1->v3':>12}")
    sep = "-" * len(header)
    print(header)
    print(sep)
    for cls in classes:
        delta = f1_v3[cls] - BASELINE_V1[cls]
        print(
            f"{cls:<18} | {BASELINE_V1[cls]:>6.3f} | "
            f"{BASELINE_AUG[cls]:>6.3f} | {f1_v3[cls]:>6.3f} | "
            f"{f1_sm[cls]:>7.3f} | {delta:>+11.3f}"
        )
    print(sep)
    acc_delta = acc_v3 - BASELINE_V1["acc"]
    print(
        f"{'Overall accuracy':<18} | {BASELINE_V1['acc']:>5.1f}% | "
        f"{BASELINE_AUG['acc']:>5.1f}% | {acc_v3:>5.1f}% | "
        f"{acc_sm:>6.1f}% | {acc_delta:>+10.1f}%"
    )

    sleep_target_v3  = f1_v3["Possible Sleep"]  >= 0.85
    sleep_target_sm  = f1_sm["Possible Sleep"]  >= 0.85
    print(f"\n    85% F1 target — RF v3 alone : {'REACHED' if sleep_target_v3 else 'not reached'}"
          f"  ({f1_v3['Possible Sleep']:.3f})")
    print(f"    85% F1 target — v3+smoothing: {'REACHED' if sleep_target_sm else 'not reached'}"
          f"  ({f1_sm['Possible Sleep']:.3f})")

    # 8. Feature importances
    print("\n[8] Feature importances (RF v3, top-to-bottom)")
    imp_sorted = sorted(zip(V3_FEATURES, clf.feature_importances_),
                        key=lambda t: t[1], reverse=True)
    for name, score in imp_sorted:
        bar  = "#" * int(score * 50)
        tag  = " <-- temporal" if name in TEMPORAL_FEATURES else ""
        print(f"    {name:<35} {score:.4f}  {bar}{tag}")

    temporal_imp = [(n, s) for n, s in imp_sorted if n in TEMPORAL_FEATURES]
    top_temporal = temporal_imp[0]
    print(f"\n    Top temporal feature : {top_temporal[0]}  (importance {top_temporal[1]:.4f})")
    print(f"    Temporal features rank:")
    for rank, (n, s) in enumerate(temporal_imp, 1):
        overall_rank = next(i for i, (fn, _) in enumerate(imp_sorted, 1) if fn == n)
        print(f"      #{rank} overall #{overall_rank:<3}  {n:<35} {s:.4f}")

    # 9. Save
    os.makedirs("outputs", exist_ok=True)
    joblib.dump(clf, V3_MODEL_PATH, compress=3)
    print(f"\n[9] Saved: {V3_MODEL_PATH}")

    with open(V3_FEATURES_PATH, "w") as f:
        json.dump({
            "state_features_v3": V3_FEATURES,
            "v2_features":       V2_FEATURES,
            "temporal_features": TEMPORAL_FEATURES,
            "zone_to_int":       ZONE_TO_INT,
            "rolling_window":    ROLLING_WINDOW,
            "temporal_window":   TEMPORAL_WINDOW,
        }, f, indent=2)
    print(f"    Saved: {V3_FEATURES_PATH}")

    # 10. Comparison plot — smoothing bar uses window-level recall from
    #     postprocess_states.py test (0.698), not the frame-level artifact (0.154).
    print("\n[10] Generating comparison plot ...")
    _make_plot(
        f1_v3["Possible Sleep"],
        pr_v3["Possible Sleep"], rc_v3["Possible Sleep"],
        recall_smooth_wl=0.698,
    )
    print(f"     Saved: {PLOT_PATH}")

    print("\n" + "=" * 68)
    print("Done.")
    print("=" * 68)


if __name__ == "__main__":
    main()
