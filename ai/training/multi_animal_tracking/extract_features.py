"""
extract_features.py
Reads outputs/multi_mouse_tracked.csv (YOLO + Hungarian tracker output) and
produces outputs/multi_mouse_features.csv with per-mouse kinematic features
and per-frame group social features.

Usage:
    cd multi_animal_tracking
    python extract_features.py
"""

import math
import os
import sys
from collections import deque

import numpy as np
import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────
INPUT_CSV           = "outputs/multi_mouse_tracked.csv"
OUTPUT_CSV          = "outputs/multi_mouse_features.csv"

SMOOTH_WINDOW       = 15      # frames for velocity rolling mean/std
TEMPORAL_WINDOW     = 450     # frames for slow-context rolling features (~30s)
FRAME_WIDTH         = 960     # pixels — from camera config
FRAME_HEIGHT        = 720     # pixels
ZONE_COLS           = 3
ZONE_ROWS           = 3
CLUSTER_DIST_PX     = 80      # px — mice closer than this count as clustered

ZONE_NAMES = [
    ["TopLeft",  "TopCenter",  "TopRight"],
    ["MidLeft",  "Center",     "MidRight"],
    ["BotLeft",  "BotCenter",  "BotRight"],
]

OUTPUT_HEADER = [
    "frame", "timestamp_s", "mouse_id", "cx", "cy", "distance_px",
    "velocity_smooth", "velocity_std",
    "quiet_streak", "max_quiet_streak_in_window", "pct_inactive_in_window",
    "activity_transitions", "zone",
    "active_mice", "mean_interanimal_dist", "min_interanimal_dist",
    "max_interanimal_dist", "spatial_spread", "cluster_count", "isolated_count",
]


# ── Zone helper ───────────────────────────────────────────────────────────────
def _assign_zone(cx, cy):
    col = 0 if cx < FRAME_WIDTH / ZONE_COLS else (
          1 if cx < 2 * FRAME_WIDTH / ZONE_COLS else 2)
    row = 0 if cy < FRAME_HEIGHT / ZONE_ROWS else (
          1 if cy < 2 * FRAME_HEIGHT / ZONE_ROWS else 2)
    return ZONE_NAMES[row][col]


# ── Per-mouse feature extractor ───────────────────────────────────────────────
def _per_mouse_features(group: pd.DataFrame) -> pd.DataFrame:
    """
    Compute kinematic features for a single mouse (one group from groupby mouse_id).
    group is sorted by frame ascending.
    """
    dist = group["distance_px"].values.astype(float)
    n    = len(dist)

    vel_smooth = np.full(n, np.nan)
    vel_std    = np.full(n, np.nan)
    qs         = np.zeros(n, dtype=int)
    max_qs_win = np.zeros(n, dtype=int)
    pct_ia_win = np.zeros(n, dtype=float)
    act_trans  = np.zeros(n, dtype=int)

    # Initialise deques
    smooth_dq  = deque(maxlen=SMOOTH_WINDOW)
    qs_dq      = deque(maxlen=TEMPORAL_WINDOW)   # quiet_streak values
    ia_dq      = deque(maxlen=TEMPORAL_WINDOW)   # is_inactive flags (0/1)

    cur_streak = 0
    for i, d in enumerate(dist):
        # Rolling velocity
        smooth_dq.append(d)
        if len(smooth_dq) >= SMOOTH_WINDOW:
            vel_smooth[i] = float(np.mean(smooth_dq))
            vel_std[i]    = float(np.std(smooth_dq))

        # Quiet streak
        if d == 0.0:
            cur_streak += 1
        else:
            cur_streak = 0
        qs[i] = cur_streak

        # Temporal context window
        qs_dq.append(cur_streak)
        ia_dq.append(1 if d == 0.0 else 0)

        max_qs_win[i] = int(max(qs_dq))
        pct_ia_win[i] = float(sum(ia_dq)) / len(ia_dq)

        # Activity transitions (abs diff of inactive flags in window)
        ia_list = list(ia_dq)
        act_trans[i] = int(sum(abs(ia_list[j] - ia_list[j-1])
                               for j in range(1, len(ia_list))))

    out = group[["frame", "timestamp_s", "mouse_id", "cx", "cy",
                 "distance_px"]].copy()
    out["velocity_smooth"]           = vel_smooth
    out["velocity_std"]              = vel_std
    out["quiet_streak"]              = qs
    out["max_quiet_streak_in_window"]= max_qs_win
    out["pct_inactive_in_window"]    = pct_ia_win
    out["activity_transitions"]      = act_trans
    out["zone"]                      = [_assign_zone(r.cx, r.cy)
                                        for r in group.itertuples(index=False)]
    return out


# ── Per-frame group feature extractor ────────────────────────────────────────
def _group_features_for_frame(subdf: pd.DataFrame) -> dict:
    """
    Compute social / group features for all mice present in a single frame.
    """
    n        = len(subdf)
    active   = int((subdf["distance_px"] > 0).sum())
    xs       = subdf["cx"].values.astype(float)
    ys       = subdf["cy"].values.astype(float)

    if n < 2:
        return {
            "active_mice":          active,
            "mean_interanimal_dist": np.nan,
            "min_interanimal_dist":  np.nan,
            "max_interanimal_dist":  np.nan,
            "spatial_spread":        0.0 if n == 1 else np.nan,
            "cluster_count":         0,
            "isolated_count":        n,
        }

    # Pairwise distances (upper triangle)
    dists = []
    for i in range(n):
        for j in range(i + 1, n):
            dists.append(math.hypot(xs[i] - xs[j], ys[i] - ys[j]))

    # Cluster / isolated counts
    has_neighbour = [False] * n
    for i in range(n):
        for j in range(n):
            if i != j and math.hypot(xs[i]-xs[j], ys[i]-ys[j]) < CLUSTER_DIST_PX:
                has_neighbour[i] = True
                break
    cluster_count  = int(sum(has_neighbour))
    isolated_count = n - cluster_count

    return {
        "active_mice":           active,
        "mean_interanimal_dist": float(np.mean(dists)),
        "min_interanimal_dist":  float(np.min(dists)),
        "max_interanimal_dist":  float(np.max(dists)),
        "spatial_spread":        float(np.std(np.concatenate([xs, ys]))),
        "cluster_count":         cluster_count,
        "isolated_count":        isolated_count,
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.makedirs("outputs", exist_ok=True)

    if not os.path.exists(INPUT_CSV):
        print(f"ERROR: input not found: {INPUT_CSV}")
        sys.exit(1)

    print(f"Loading {INPUT_CSV} ...")
    df = pd.read_csv(INPUT_CSV, on_bad_lines='skip')
    print(f"  {len(df):,} rows raw (malformed lines skipped)")

    # Clean: remove unmatched "?" detections and any NaN mouse_ids from bad lines
    df = df[df['mouse_id'].notna() & (df['mouse_id'] != '?')]
    print(f"After removing '?' and NaN: {len(df):,} rows")
    print(f"Track IDs: {sorted(df.mouse_id.unique())}")
    print(f"Detections per ID:")
    print(df.mouse_id.value_counts().sort_index().to_string())
    print()
    print(f"  {len(df):,} rows,  {df['mouse_id'].nunique()} unique mouse IDs, "
          f"{df['frame'].nunique():,} frames")

    # ── Per-mouse features ────────────────────────────────────────────────────
    print("Computing per-mouse kinematic features ...")
    parts = []
    for mid, grp in df.sort_values("frame").groupby("mouse_id", sort=False):
        parts.append(_per_mouse_features(grp.reset_index(drop=True)))
    per_mouse = pd.concat(parts, ignore_index=True).sort_values(
        ["frame", "mouse_id"])

    # ── Group features per frame ──────────────────────────────────────────────
    print("Computing per-frame group social features ...")
    group_rows = []
    for frame_val, subdf in df.groupby("frame"):
        gf = _group_features_for_frame(subdf)
        gf["frame"] = frame_val
        group_rows.append(gf)
    group_df = pd.DataFrame(group_rows)

    # ── Join ──────────────────────────────────────────────────────────────────
    out = per_mouse.merge(group_df, on="frame", how="left")
    out = out[OUTPUT_HEADER]

    out.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved: {OUTPUT_CSV}  ({len(out):,} rows)")
    print("\nFirst 5 rows:")
    print(out.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
