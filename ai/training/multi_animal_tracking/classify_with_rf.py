"""
classify_with_rf.py
Apply the existing RF v3 classifier (trained on single-mouse data, 99.6% accuracy)
to each individually tracked mouse in multi_mouse_tracked.csv.

Inference only — no retraining. Each mouse's centroid trajectory is processed
independently to extract the same 16 temporal features used during training.

Usage:
    cd multi_animal_tracking
    python classify_with_rf.py
"""

import json
import os
import sys
from collections import deque

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ── Model paths (absolute — RF v3 lives in behavioral_monitoring/) ────────────
RF_MODEL_PATH  = (r"C:\Users\grazv\Desktop\Behavioral Monitoring System"
                  r"\behavioral_monitoring\outputs\state_classifier_v3.pkl")
FEATURE_COLS_PATH = (r"C:\Users\grazv\Desktop\Behavioral Monitoring System"
                     r"\behavioral_monitoring\outputs\feature_columns_v3.json")

# ── Data paths (relative — run from multi_animal_tracking/) ──────────────────
TRACKED_CSV    = "outputs/multi_mouse_tracked.csv"
OUTPUT_CSV     = "outputs/multi_mouse_rf_classified.csv"
PLOT_PATH      = "outputs/plots/rf_vs_threshold_multi_mouse.png"

# ── Feature constants (must match training config exactly) ────────────────────
SMOOTH_WINDOW       = 15      # rolling mean/std window for velocity
TEMPORAL_WINDOW     = 450     # rolling window for slow-context features
WAKE_THRESHOLD      = 2.5     # px/frame — movement_smooth above this = active
INACTIVITY_THRESH   = 2.5     # px/frame — below this = inactive frame
POSSIBLE_SLEEP_MIN  = 450     # consecutive quiet frames → Possible Sleep

# ── Zone config (must match training) ────────────────────────────────────────
FRAME_WIDTH   = 960
FRAME_HEIGHT  = 720
ZONE_TO_INT   = {
    "TopLeft": 0, "TopCenter": 1, "TopRight": 2,
    "MidLeft": 3, "Center":    4, "MidRight": 5,
    "BotLeft": 6, "BotCenter": 7, "BotRight": 8,
}

# ── Plot constants ────────────────────────────────────────────────────────────
TOP_N_MICE    = 5
FIG_SIZE      = (12, 5)
DPI           = 150
STATES        = ["Wake / Active", "Quiet / Rest", "Possible Sleep"]
STATE_COLORS  = {"Wake / Active": "#4CAF50",
                 "Quiet / Rest":  "#FFC107",
                 "Possible Sleep":"#2196F3"}


# ── Helper: assign zone ───────────────────────────────────────────────────────
def _zone_enc(cx, cy):
    col = 0 if cx < FRAME_WIDTH/3  else (1 if cx < 2*FRAME_WIDTH/3  else 2)
    row = 0 if cy < FRAME_HEIGHT/3 else (1 if cy < 2*FRAME_HEIGHT/3 else 2)
    names = [["TopLeft","TopCenter","TopRight"],
             ["MidLeft","Center",   "MidRight"],
             ["BotLeft","BotCenter","BotRight"]]
    return ZONE_TO_INT[names[row][col]]


# ── Feature extraction (per mouse, sequential) ────────────────────────────────
def build_rf_features(mouse_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute the 16 RF v3 features from a single mouse's trajectory.
    mouse_df must be sorted by frame ascending and contain:
      distance_px, cx, cy, bbox_x1, bbox_y1, bbox_x2, bbox_y2
    Returns a DataFrame with the 16 feature columns (NaN where window not full).
    """
    n    = len(mouse_df)
    dist = mouse_df["distance_px"].values.astype(np.float32)
    cx   = mouse_df["cx"].values.astype(np.float32)
    cy   = mouse_df["cy"].values.astype(np.float32)
    # bbox area as motion_area proxy
    area = ((mouse_df["bbox_x2"] - mouse_df["bbox_x1"]) *
            (mouse_df["bbox_y2"] - mouse_df["bbox_y1"])).values.astype(np.float32)

    # Output arrays
    movement_smooth     = np.full(n, np.nan, dtype=np.float32)
    is_inactive         = np.zeros(n, dtype=np.float32)
    dist_roll_mean      = np.full(n, np.nan, dtype=np.float32)
    dist_roll_std       = np.full(n, np.nan, dtype=np.float32)
    area_roll_mean      = np.full(n, np.nan, dtype=np.float32)
    zone_enc_arr        = np.zeros(n, dtype=np.float32)
    quiet_streak_arr    = np.zeros(n, dtype=np.float32)
    max_qs_win          = np.zeros(n, dtype=np.float32)
    pct_ia_win          = np.zeros(n, dtype=np.float32)
    act_trans           = np.zeros(n, dtype=np.float32)

    dist_dq   = deque(maxlen=SMOOTH_WINDOW)
    area_dq   = deque(maxlen=SMOOTH_WINDOW)
    qs_dq     = deque(maxlen=TEMPORAL_WINDOW)
    ia_dq     = deque(maxlen=TEMPORAL_WINDOW)

    cur_streak = 0
    for i in range(n):
        dist_dq.append(dist[i])
        area_dq.append(area[i])

        if len(dist_dq) >= SMOOTH_WINDOW:
            mv = float(np.mean(dist_dq))
            movement_smooth[i]  = mv
            dist_roll_mean[i]   = mv
            dist_roll_std[i]    = float(np.std(dist_dq))
            area_roll_mean[i]   = float(np.mean(area_dq))
            is_inactive[i]      = 1.0 if mv < INACTIVITY_THRESH else 0.0
        else:
            is_inactive[i] = 1.0 if dist[i] == 0.0 else 0.0

        # quiet streak (frame-level: distance == 0)
        cur_streak = (cur_streak + 1) if dist[i] == 0.0 else 0
        quiet_streak_arr[i] = float(cur_streak)

        zone_enc_arr[i] = _zone_enc(cx[i], cy[i])

        qs_dq.append(float(cur_streak))
        ia_dq.append(float(is_inactive[i]))

        max_qs_win[i]   = float(max(qs_dq))
        pct_ia_win[i]   = float(sum(ia_dq)) / len(ia_dq)
        ia_list         = list(ia_dq)
        act_trans[i]    = float(sum(abs(ia_list[k] - ia_list[k-1])
                                    for k in range(1, len(ia_list))))

    out = pd.DataFrame(index=mouse_df.index)
    out["distance_from_previous_px"] = dist
    out["movement_smooth"]           = movement_smooth
    out["is_inactive_smooth_int"]    = is_inactive
    out["motion_area"]               = area
    out["dist_roll_mean_15"]         = dist_roll_mean
    out["dist_roll_std_15"]          = dist_roll_std
    out["area_roll_mean_15"]         = area_roll_mean
    out["zone_enc"]                  = zone_enc_arr
    out["quiet_streak"]              = quiet_streak_arr
    out["distance_x_motion_area"]    = dist * area
    out["cv_movement"]               = dist_roll_std / (dist_roll_mean + 1e-6)
    out["quiet_streak_sq"]           = quiet_streak_arr ** 2
    out["movement_smooth_sq"]        = movement_smooth ** 2
    out["max_quiet_streak_in_window"]= max_qs_win
    out["pct_inactive_in_window"]    = pct_ia_win
    out["activity_transitions"]      = act_trans
    return out


# ── Threshold classifier (mirrors single-mouse pipeline) ─────────────────────
def threshold_state(movement_smooth, quiet_streak):
    if np.isnan(movement_smooth) or movement_smooth > WAKE_THRESHOLD:
        return "Wake / Active"
    elif quiet_streak >= POSSIBLE_SLEEP_MIN:
        return "Possible Sleep"
    else:
        return "Quiet / Rest"


# ── Comparison table ──────────────────────────────────────────────────────────
def _pct(series, value):
    return round(series.eq(value).mean() * 100, 1)

def print_comparison(result_df):
    header = (f"{'Mouse':<7} | "
              f"{'Thr Wake':>8} {'RF Wake':>7} | "
              f"{'Thr Rest':>8} {'RF Rest':>7} | "
              f"{'Thr Sleep':>9} {'RF Sleep':>8} | "
              f"{'Agree':>6}")
    print(header)
    print("-" * len(header))

    mouse_ids = sorted(result_df["mouse_id"].unique())
    for mid in mouse_ids:
        sub = result_df[result_df["mouse_id"] == mid].dropna(subset=["rf_state"])
        if sub.empty:
            continue
        agree = round((sub["threshold_state"] == sub["rf_state"]).mean() * 100, 1)
        print(
            f"{mid:<7} | "
            f"{_pct(sub['threshold_state'], 'Wake / Active'):>8.1f} "
            f"{_pct(sub['rf_state'],        'Wake / Active'):>7.1f} | "
            f"{_pct(sub['threshold_state'], 'Quiet / Rest'):>8.1f} "
            f"{_pct(sub['rf_state'],        'Quiet / Rest'):>7.1f} | "
            f"{_pct(sub['threshold_state'], 'Possible Sleep'):>9.1f} "
            f"{_pct(sub['rf_state'],        'Possible Sleep'):>8.1f} | "
            f"{agree:>6.1f}%"
        )


# ── Comparison plot ───────────────────────────────────────────────────────────
def make_comparison_plot(result_df, top_mice, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    n_mice   = len(top_mice)
    n_states = len(STATES)
    bar_w    = 0.35
    x_base   = np.arange(n_mice)

    fig, axes = plt.subplots(1, n_states, figsize=FIG_SIZE, sharey=False)

    for si, state in enumerate(STATES):
        ax = axes[si]
        thr_vals = []
        rf_vals  = []
        for mid in top_mice:
            sub = result_df[result_df["mouse_id"] == mid].dropna(subset=["rf_state"])
            thr_vals.append(_pct(sub["threshold_state"], state))
            rf_vals.append(_pct(sub["rf_state"],         state))

        c = STATE_COLORS[state]
        ax.bar(x_base - bar_w/2, thr_vals, bar_w, label="Threshold",
               color=c, alpha=0.55, edgecolor="grey", linewidth=0.5)
        ax.bar(x_base + bar_w/2, rf_vals,  bar_w, label="RF v3",
               color=c, alpha=0.92, edgecolor="black", linewidth=0.8)

        for xc, tv, rv in zip(x_base, thr_vals, rf_vals):
            ax.text(xc - bar_w/2, tv + 0.5, f"{tv:.0f}%",
                    ha="center", va="bottom", fontsize=7, color="grey")
            ax.text(xc + bar_w/2, rv + 0.5, f"{rv:.0f}%",
                    ha="center", va="bottom", fontsize=7)

        ax.set_xticks(x_base)
        ax.set_xticklabels([f"Mouse {m}" for m in top_mice], fontsize=9)
        ax.set_title(state, fontsize=10, fontweight="bold",
                     color=STATE_COLORS[state])
        ax.set_ylabel("% of frames", fontsize=9)
        ax.set_ylim(0, 100)
        ax.spines[["top", "right"]].set_visible(False)
        if si == 0:
            ax.legend(fontsize=8)

    fig.suptitle("RF v3 vs Threshold predictions — per mouse, per state\n"
                 "(faded bar = threshold, solid bar = RF v3)",
                 fontsize=11, fontweight="bold")
    plt.tight_layout()
    fig.savefig(out_path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.makedirs("outputs/plots", exist_ok=True)

    # Load RF v3
    if not os.path.exists(RF_MODEL_PATH):
        print(f"ERROR: RF model not found: {RF_MODEL_PATH}")
        sys.exit(1)
    rf = joblib.load(RF_MODEL_PATH)
    with open(FEATURE_COLS_PATH) as f:
        meta = json.load(f)
    feature_cols = meta["state_features_v3"]
    print(f"RF v3 loaded: {len(feature_cols)} features")
    print(f"Features: {feature_cols}\n")

    # Load tracked CSV
    if not os.path.exists(TRACKED_CSV):
        print(f"ERROR: tracked CSV not found: {TRACKED_CSV}")
        sys.exit(1)
    print(f"Loading {TRACKED_CSV} ...")
    df = pd.read_csv(TRACKED_CSV, on_bad_lines="skip")
    df = df[df["mouse_id"].notna() & (df["mouse_id"] != "?")]
    df["frame"]       = pd.to_numeric(df["frame"],       errors="coerce")
    df["distance_px"] = pd.to_numeric(df["distance_px"], errors="coerce").fillna(0.0)
    for col in ["cx", "cy", "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["frame"])
    print(f"  {len(df):,} rows,  {df['mouse_id'].nunique()} track IDs")

    # Select top N mice
    top_mice = (df.groupby("mouse_id").size()
                .nlargest(TOP_N_MICE).index.tolist())
    print(f"  Top {TOP_N_MICE} mice: {top_mice}\n")
    df = df[df["mouse_id"].isin(top_mice)]

    # Process each mouse independently
    all_results = []
    for mid in top_mice:
        print(f"Processing Mouse {mid} ...", end=" ", flush=True)
        sub = (df[df["mouse_id"] == mid]
               .sort_values("frame")
               .reset_index(drop=True))

        # Build the 16 RF v3 features
        feats = build_rf_features(sub)

        # Threshold state (vectorised)
        sub["threshold_state"] = [
            threshold_state(feats["movement_smooth"].iloc[i],
                            feats["quiet_streak"].iloc[i])
            for i in range(len(sub))
        ]

        # RF v3 inference — only on rows where all features are non-NaN
        X_all    = feats[feature_cols].values.astype(np.float32)
        valid    = ~np.isnan(X_all).any(axis=1)
        rf_preds = np.full(len(sub), None, dtype=object)
        if valid.sum() > 0:
            rf_preds[valid] = rf.predict(X_all[valid])

        sub["rf_state"]   = rf_preds
        sub["mouse_id"]   = mid

        # Attach key feature columns for output CSV
        for col in feature_cols:
            sub[f"feat_{col}"] = feats[col].values

        valid_n  = int(valid.sum())
        agree_n  = int((sub["threshold_state"] == sub["rf_state"]).sum())
        pct_agree = round(agree_n / max(valid_n, 1) * 100, 1)
        print(f"{len(sub):,} frames  |  {valid_n:,} classified  |  {pct_agree}% agreement")
        all_results.append(sub)

    result_df = pd.concat(all_results, ignore_index=True)

    # Save CSV
    out_cols = (["frame", "timestamp_s", "mouse_id", "cx", "cy",
                 "conf", "distance_px", "threshold_state", "rf_state"]
                + [f"feat_{c}" for c in feature_cols])
    result_df[out_cols].to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved: {OUTPUT_CSV}  ({len(result_df):,} rows)")

    # Comparison table
    print("\n" + "=" * 78)
    print("RF v3 vs Threshold comparison (all classified frames):")
    print("=" * 78)
    print_comparison(result_df)

    # Comparison plot
    print()
    make_comparison_plot(result_df, top_mice, PLOT_PATH)


if __name__ == "__main__":
    main()
