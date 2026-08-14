"""
online_classifier.py
Teacher-student online learning: RF classifier (teacher) → SGDClassifier (student).

The RF provides per-frame state labels. The SGD receives those labels in windows
of ONLINE_WINDOW_SIZE rows and updates its weights via partial_fit().  Agreement
with the RF teacher is tracked across all windows.

Architecture:
    RF teacher  →  per-frame labels
         ↓
    SGD student  ←  partial_fit(X_window, y_rf_window)
         ↓
    SGD predictions improve over time (predict-then-update protocol)

Run: python online_classifier.py
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
from sklearn.linear_model import SGDClassifier
from sklearn.utils.class_weight import compute_class_weight
from collections import Counter

from behavior_analysis import FPS, WAKE_THRESHOLD, RECORDING_START

# ── Paths ─────────────────────────────────────────────────────────────────────
TRACKING_CSV         = "outputs/tracking_data.csv"
STATE_MODEL_PATH     = "outputs/state_classifier.pkl"
FEATURE_COLUMNS_PATH = "outputs/feature_columns.json"
SGD_MODEL_PATH       = "outputs/sgd_online_classifier.pkl"
LEARNING_LOG_PATH    = "outputs/sgd_learning_log.csv"

# ── Online learning constants ─────────────────────────────────────────────────
ONLINE_WINDOW_SIZE      = 450    # frames per update window (30s at 15 FPS FRAME_SKIP=2)
MIN_WINDOWS_BEFORE_EVAL = 10     # warm-up period before tracking agreement
SGD_LEARNING_RATE       = "optimal"
ALL_CLASSES             = ["Wake / Active", "Quiet / Rest", "Possible Sleep"]

# ── Recording start — parse HH:MM:SS from "YYYY HH:MM:SS" ────────────────────
_REC_HMS      = RECORDING_START.split(" ")[1]          # "15:17:30"
_rh, _rm, _rs = _REC_HMS.split(":")
_REC_START_S  = int(_rh) * 3600 + int(_rm) * 60 + int(_rs)    # 54450


def _frame_to_clock(frame_num: int) -> str:
    """Video frame number → wall-clock 'HH:MM:SS'."""
    total = (_REC_START_S + frame_num / FPS) % 86400
    h = int(total) // 3600
    m = int(total % 3600) // 60
    s = int(total % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_features(df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    """
    Mirror train_classifier.py build_features() exactly.
    Rolling features are computed over the FULL dataframe so windowed views
    retain correct context — no re-computation per window.
    """
    rw          = meta["rolling_window"]
    zone_to_int = meta["zone_to_int"]

    out = pd.DataFrame(index=df.index)
    out["distance_from_previous_px"] = df["distance_from_previous_px"]
    out["movement_smooth"]           = df["movement_smooth"]
    out["is_inactive_smooth_int"]    = df["is_inactive_smooth"].astype(int)
    out["motion_area"]               = df["motion_area"]

    out["dist_roll_mean_15"] = (df["distance_from_previous_px"]
                               .rolling(rw, min_periods=rw).mean())
    out["dist_roll_std_15"]  = (df["distance_from_previous_px"]
                               .rolling(rw, min_periods=rw).std())
    out["area_roll_mean_15"] = (df["motion_area"]
                               .rolling(rw, min_periods=rw).mean())

    out["zone_enc"] = df["zone"].map(zone_to_int)

    if "quiet_streak" in df.columns:
        out["quiet_streak"] = df["quiet_streak"].astype(float)
    else:
        streak, streaks = 0, []
        for ms in df["movement_smooth"]:
            streak = (streak + 1) if (pd.isna(ms) or ms < WAKE_THRESHOLD) else 0
            streaks.append(float(streak))
        out["quiet_streak"] = streaks

    return out


def main():
    print("=" * 64)
    print("Online Classifier  |  RF Teacher -> SGD Student")
    print("=" * 64)

    # ── 1. Load data ──────────────────────────────────────────────────────────
    print(f"\n[1] Loading {TRACKING_CSV} ...")
    df = pd.read_csv(TRACKING_CSV)
    print(f"    {len(df):,} rows   columns: {list(df.columns)}")

    # ── 2. Load feature manifest + RF teacher ─────────────────────────────────
    print(f"\n[2] Loading meta + RF teacher ...")
    with open(FEATURE_COLUMNS_PATH) as f:
        meta = json.load(f)
    state_feats = meta["state_features"]    # 9 features incl. quiet_streak

    rf_clf = joblib.load(STATE_MODEL_PATH)
    print(f"    RF n_features: {rf_clf.n_features_in_}   "
          f"features: {state_feats}")

    # ── 3. Feature engineering (full dataframe, single pass) ──────────────────
    print("\n[3] Engineering features ...")
    feat_df    = _build_features(df, meta)
    valid_mask = feat_df[state_feats].notna().all(axis=1)
    feat_clean = feat_df[valid_mask]
    X_all      = feat_clean[state_feats].values.astype(np.float32)
    frames_all = df[valid_mask]["frame"].values

    dropped = (~valid_mask).sum()
    print(f"    {len(X_all):,} valid rows  ({dropped} dropped — rolling window head)")

    # ── 4. RF teacher: batch predict once (avoid repeated inference per window) -
    print("\n[4] RF teacher predicting all rows ...")
    t0 = time.perf_counter()
    y_rf_all = rf_clf.predict(X_all)
    print(f"    Done in {time.perf_counter() - t0:.1f}s")
    rf_counts = Counter(y_rf_all)
    for cls in ALL_CLASSES:
        pct = rf_counts.get(cls, 0) / len(y_rf_all) * 100
        print(f"      {cls:<18} {pct:5.1f}%")

    # ── 5. Initialize SGD student ─────────────────────────────────────────────
    # class_weight='balanced' is unsupported for partial_fit — compute weights
    # from the full RF label distribution upfront and pass as a dict.
    print("\n[5] Initializing SGD student ...")
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
    n_total   = len(X_all)
    n_windows = n_total // ONLINE_WINDOW_SIZE
    print(f"\n[6] Streaming {n_windows} windows x {ONLINE_WINDOW_SIZE} rows "
          f"({ONLINE_WINDOW_SIZE / FPS:.0f}s per window)  "
          f"warm-up: {MIN_WINDOWS_BEFORE_EVAL} windows ...")

    log_rows          = []
    cumulative_agree  = 0
    is_fitted         = False

    for w in range(n_windows):
        start   = w * ONLINE_WINDOW_SIZE
        end     = start + ONLINE_WINDOW_SIZE
        X_w     = X_all[start:end]
        y_rf_w  = y_rf_all[start:end]
        frame_w = int(frames_all[start])
        clock   = _frame_to_clock(frame_w)

        # Majority-vote RF label for this window (logging only)
        rf_label = Counter(y_rf_w).most_common(1)[0][0]

        # ── Predict BEFORE updating (honest evaluation) ───────────────────────
        if is_fitted:
            y_sgd_w    = sgd.predict(X_w)
            probs_w    = sgd.predict_proba(X_w)
            sgd_label  = Counter(y_sgd_w).most_common(1)[0][0]
            confidence = float(np.mean(np.max(probs_w, axis=1)))
        else:
            # Before first partial_fit: placeholder — SGD not yet initialized
            sgd_label  = ALL_CLASSES[0]
            confidence = 0.0

        # ── Update SGD with RF teacher labels ─────────────────────────────────
        sgd.partial_fit(X_w, y_rf_w, classes=ALL_CLASSES)
        is_fitted = True

        agree = bool(sgd_label == rf_label)

        # Cumulative agreement tracks only post-warm-up windows
        if w >= MIN_WINDOWS_BEFORE_EVAL:
            cumulative_agree += int(agree)
        windows_post_warmup  = max(1, w - MIN_WINDOWS_BEFORE_EVAL + 1)
        cum_agree_pct = (cumulative_agree / windows_post_warmup * 100
                         if w >= MIN_WINDOWS_BEFORE_EVAL else 0.0)

        log_rows.append({
            "window":                   w,
            "timestamp_clock":          clock,
            "rf_label":                 rf_label,
            "sgd_label":                sgd_label,
            "agreement":                agree,
            "confidence":               round(confidence, 4),
            "cumulative_agreement_pct": round(cum_agree_pct, 2),
        })

        # Print every 10 windows
        if w % 10 == 0 or w < 3:
            warmup_tag = " [WARM-UP]" if w < MIN_WINDOWS_BEFORE_EVAL else ""
            if w >= MIN_WINDOWS_BEFORE_EVAL and w >= 10:
                recent     = log_rows[max(0, w - 9): w + 1]
                drift_pct  = (1 - sum(r["agreement"] for r in recent) / len(recent)) * 100
                drift_tag  = f" | Drift: {drift_pct:.0f}%"
            else:
                drift_tag  = ""
            print(
                f"[{clock}] Window {w:>4} — "
                f"RF: {rf_label:<16} | SGD: {sgd_label:<16} | "
                f"Agreement: {str(agree):<5} | Confidence: {confidence:.2f}"
                f"{drift_tag}{warmup_tag}"
            )

    # ── 7. Save SGD model ─────────────────────────────────────────────────────
    os.makedirs("outputs", exist_ok=True)
    print(f"\n[7] Saving SGD model -> {SGD_MODEL_PATH} ...")
    joblib.dump(sgd, SGD_MODEL_PATH, compress=3)
    size_kb = os.path.getsize(SGD_MODEL_PATH) / 1024
    print(f"    {size_kb:.0f} KB")

    # ── 8. Save learning log ──────────────────────────────────────────────────
    log_df = pd.DataFrame(log_rows)
    log_df.to_csv(LEARNING_LOG_PATH, index=False)
    print(f"[8] Learning log -> {LEARNING_LOG_PATH}  ({len(log_df)} rows)")

    # ── 9. Plots ──────────────────────────────────────────────────────────────
    print("\n[9] Generating plots ...")
    _generate_plots(log_df)

    # ── 10. Report ────────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("RESULTS")
    print("=" * 64)
    final_agree = log_df["cumulative_agreement_pct"].iloc[-1]
    conf_w1     = log_df["confidence"].iloc[1] if len(log_df) > 1 else 0.0
    conf_end    = log_df["confidence"].iloc[-1]
    print(f"  Final cumulative agreement  : {final_agree:.1f}%")
    print(f"  Confidence window 1         : {conf_w1:.4f}")
    print(f"  Confidence final window     : {conf_end:.4f}")
    print(f"  Windows processed           : {len(log_df)}")

    print(f"\n  First 3 rows of sgd_learning_log.csv:")
    print(log_df.head(3).to_string(index=False))
    print(f"\n  Last 3 rows of sgd_learning_log.csv:")
    print(log_df.tail(3).to_string(index=False))
    print("=" * 64)


def _generate_plots(log_df: pd.DataFrame) -> None:
    """Produce agreement-over-time and confidence-over-time line charts."""
    import matplotlib.dates as mdates
    from datetime import datetime, timedelta

    os.makedirs("outputs/plots", exist_ok=True)

    # ── Convert timestamp_clock strings to datetime, handling midnight rollover ─
    # Recording starts at 15:17:30.  Times >= 15:17:30 are on base day;
    # times < 15:17:30 have crossed midnight and belong to the next day.
    BASE_DATE       = datetime(2024, 1, 1)
    REC_START_SECS  = _REC_START_S     # 54450  (imported constant from module scope)

    def _clock_to_dt(hms: str) -> datetime:
        h, m, s = hms.split(":")
        cs = int(h) * 3600 + int(m) * 60 + int(s)
        rel = cs - REC_START_SECS if cs >= REC_START_SECS else 86400 - REC_START_SECS + cs
        return BASE_DATE + timedelta(seconds=rel)

    x            = log_df["timestamp_clock"].apply(_clock_to_dt)
    warmup_end_x = x.iloc[min(MIN_WINDOWS_BEFORE_EVAL, len(x) - 1)]

    # ── Shared axis formatter / locator ───────────────────────────────────────
    locator   = mdates.HourLocator(interval=2)
    formatter = mdates.DateFormatter("%H:%M")

    # ── Plot 1: cumulative agreement ──────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.axvspan(x.iloc[0], warmup_end_x, color="gray", alpha=0.15,
               label=f"Warm-up ({MIN_WINDOWS_BEFORE_EVAL} windows)")
    ax.plot(x, log_df["cumulative_agreement_pct"],
            color="steelblue", linewidth=2.0, label="Cumulative agreement %")
    ax.axhline(90, color="darkorange", linestyle="--", linewidth=1.5,
               label="90% target")
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=9)
    ax.set_xlabel("Time of day", fontsize=11)
    ax.set_ylabel("Agreement with RF teacher (%)", fontsize=11)
    ax.set_ylim(0, 105)
    ax.set_xlim(x.iloc[0], x.iloc[-1])
    ax.set_title("SGD online classifier learns to match RF teacher over time",
                 fontsize=13, fontweight="bold", pad=12)
    ax.legend(fontsize=9, framealpha=0.8)
    ax.grid(alpha=0.3, linestyle="--")
    plt.tight_layout()
    p1 = "outputs/plots/sgd_agreement_over_time.png"
    plt.savefig(p1, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    {p1}  ({os.path.getsize(p1) // 1024} KB)")

    # ── Plot 2: per-window confidence ─────────────────────────────────────────
    conf       = log_df["confidence"].values
    smooth_w   = max(1, min(15, len(log_df) // 10))
    smooth_conf = pd.Series(conf).rolling(smooth_w, center=True).mean().values

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.axvspan(x.iloc[0], warmup_end_x, color="gray", alpha=0.15, label="Warm-up")
    ax.plot(x, conf, color="seagreen", linewidth=1.0, alpha=0.5,
            label="Per-window confidence")
    ax.plot(x, smooth_conf, color="darkgreen", linewidth=2.5,
            label=f"Smoothed (window={smooth_w})")
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right", fontsize=9)
    ax.set_xlabel("Time of day", fontsize=11)
    ax.set_ylabel("Prediction confidence (0–1)", fontsize=11)
    ax.set_ylim(0, 1.05)
    ax.set_xlim(x.iloc[0], x.iloc[-1])
    ax.set_title("Model confidence increases as it adapts to this mouse's behavior",
                 fontsize=13, fontweight="bold", pad=12)
    ax.legend(fontsize=9, framealpha=0.8)
    ax.grid(alpha=0.3, linestyle="--")
    plt.tight_layout()
    p2 = "outputs/plots/sgd_confidence_over_time.png"
    plt.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    {p2}  ({os.path.getsize(p2) // 1024} KB)")


if __name__ == "__main__":
    main()
