"""
generate_multi_plots.py
Generates 4 presentation-quality plots from the multi-mouse behavior pipeline:

  Plot 1: per_mouse_activity.png       — individual velocity profiles over time
  Plot 2: group_social_timeline.png    — stacked social behavior area chart
  Plot 3: per_mouse_state_breakdown.png — grouped bar: state % per mouse
  Plot 4: social_classifier_confusion.png — RF confusion matrix

Usage:
    cd multi_animal_tracking
    python generate_multi_plots.py
"""

import os
import sys

import matplotlib
matplotlib.use("Agg")   # must be before pyplot import
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────
FEATURES_CSV     = "outputs/multi_mouse_features.csv"
BEHAVIOR_CSV     = "outputs/multi_mouse_behavior.csv"
SOCIAL_CSV       = "outputs/group_social_summary.csv"
CLASSIFIER_PKL   = "outputs/social_classifier.pkl"
OUTPUT_DIR       = "outputs/plots"

FIG_SIZE         = (12, 6)
DPI              = 150
VIDEO_DURATION_H = 9.78    # hours — for axis label

STATE_COLORS = {
    "Wake / Active":  "#4CAF50",
    "Quiet / Rest":   "#FFC107",
    "Possible Sleep": "#2196F3",
}
SOCIAL_COLORS = {
    "Huddling":    "#5C85D6",
    "Normal":      "#AAAAAA",
    "Exploration": "#5CAD5C",
}
MOUSE_COLORS = ["#2196F3", "#FF5722", "#9C27B0", "#4CAF50", "#FF9800"]


# ── Plot 1: per-mouse activity profiles ───────────────────────────────────────
def plot_per_mouse_activity(features_df: pd.DataFrame, out_path: str):
    mouse_ids = sorted(features_df["mouse_id"].unique())
    n         = len(mouse_ids)
    if n == 0:
        print("  No mouse IDs found — skipping plot 1")
        return

    fig, axes = plt.subplots(n, 1, figsize=(14, 10), sharex=True)
    if n == 1:
        axes = [axes]

    for ax, mid, color in zip(axes, mouse_ids, MOUSE_COLORS):
        sub = features_df[features_df["mouse_id"] == mid].sort_values("timestamp_s")
        ts  = sub["timestamp_s"].values / 3600       # hours
        vs  = sub["velocity_smooth"].values

        # Shade background by state
        if "state" in sub.columns:
            for state, sc in STATE_COLORS.items():
                mask = (sub["state"] == state).values
                ax.fill_between(ts, 0, vs, where=mask, alpha=0.25,
                                color=sc, label=state)

        ax.plot(ts, vs, color=color, linewidth=0.6, alpha=0.85)
        ax.set_ylabel(f"Mouse {mid}\nvel (px)", fontsize=8)
        ax.set_ylim(bottom=0)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=7)

    axes[-1].set_xlabel("Time (hours)", fontsize=9)
    axes[0].set_title(
        "Individual mouse activity profiles — top 5 tracked mice",
        fontsize=12, fontweight="bold")

    # Legend from first axis handles (deduplicated)
    handles, labels = axes[0].get_legend_handles_labels()
    seen = {}
    for h, l in zip(handles, labels):
        if l not in seen:
            seen[l] = h
    axes[0].legend(seen.values(), seen.keys(), fontsize=8,
                   loc="upper right", framealpha=0.7)

    plt.tight_layout()
    fig.savefig(out_path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")


# ── Plot 2: group social timeline (stacked area) ──────────────────────────────
def plot_social_timeline(social_df: pd.DataFrame, out_path: str):
    if social_df.empty:
        print("  Social summary empty — skipping plot 2")
        return

    ts = social_df["timestamp_start"].values / 3600   # hours
    order = ["Huddling", "Normal", "Exploration"]

    # Build stacked data: each window contributes to one band
    # Simple approach: one-hot encode dominant class per window
    data = {k: np.zeros(len(social_df)) for k in order}
    for i, row in social_df.iterrows():
        sc = row.get("social_class", "Normal")
        if sc in data:
            data[sc][i] = 1.0

    fig, ax = plt.subplots(figsize=FIG_SIZE)
    bottom = np.zeros(len(social_df))
    for label in order:
        vals = data[label]
        ax.fill_between(ts, bottom, bottom + vals,
                        step="post", alpha=0.75,
                        color=SOCIAL_COLORS[label], label=label)
        bottom += vals

    ax.set_xlim(ts[0] if len(ts) else 0, ts[-1] if len(ts) else 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Time (hours)", fontsize=10)
    ax.set_ylabel("Social state", fontsize=10)
    ax.set_title("Group social behavior over time",
                 fontsize=12, fontweight="bold")
    ax.set_yticks([])
    ax.legend(fontsize=10, loc="upper right")
    ax.spines[["top", "right", "left"]].set_visible(False)
    plt.tight_layout()
    fig.savefig(out_path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")


# ── Plot 3: per-mouse state breakdown (grouped bar) ───────────────────────────
def plot_state_breakdown(behavior_df: pd.DataFrame, out_path: str):
    if behavior_df.empty:
        print("  Behavior data empty — skipping plot 3")
        return

    states    = ["Wake / Active", "Quiet / Rest", "Possible Sleep"]
    mouse_ids = sorted(behavior_df["mouse_id"].unique())
    n_mice    = len(mouse_ids)
    n_states  = len(states)

    # Compute % per mouse per state
    pcts = {}
    for mid in mouse_ids:
        sub   = behavior_df[behavior_df["mouse_id"] == mid]
        total = len(sub)
        pcts[mid] = {s: sub[sub["state"] == s].shape[0] / total * 100
                     for s in states}

    x     = np.arange(n_mice)
    w     = 0.25
    fig, ax = plt.subplots(figsize=FIG_SIZE)

    for si, state in enumerate(states):
        offset = (si - 1) * w
        vals   = [pcts[mid][state] for mid in mouse_ids]
        bars   = ax.bar(x + offset, vals, w, label=state,
                        color=STATE_COLORS[state], alpha=0.85)
        for bar, val in zip(bars, vals):
            if val > 3:
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + 0.5,
                        f"{val:.0f}%", ha="center", va="bottom", fontsize=7)

    ax.set_xticks(x)
    ax.set_xticklabels([f"Mouse {m}" for m in mouse_ids], fontsize=10)
    ax.set_ylabel("Percentage of frames (%)", fontsize=10)
    ax.set_ylim(0, 105)
    ax.set_title("Behavioral state per individual mouse (5 mice)",
                 fontsize=12, fontweight="bold")
    ax.legend(fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    plt.tight_layout()
    fig.savefig(out_path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")


# ── Plot 4: social classifier confusion matrix ────────────────────────────────
def plot_confusion_matrix(out_path: str):
    if not os.path.exists(CLASSIFIER_PKL):
        print(f"  {CLASSIFIER_PKL} not found — skipping plot 4")
        return
    if not os.path.exists(FEATURES_CSV):
        print(f"  {FEATURES_CSV} not found — skipping plot 4")
        return

    import joblib
    from sklearn.metrics import ConfusionMatrixDisplay, confusion_matrix
    from sklearn.model_selection import train_test_split

    SOCIAL_FEATURES = [
        "mean_interanimal_dist", "min_interanimal_dist",
        "spatial_spread", "cluster_count", "isolated_count", "active_mice",
    ]
    HUDDLE_T  = 80.0
    EXPLORE_T = 200.0

    def _label(d):
        if np.isnan(d): return None
        if d < HUDDLE_T:   return "Huddling"
        if d > EXPLORE_T:  return "Exploration"
        return "Normal"

    df = pd.read_csv(FEATURES_CSV)
    grp = df.groupby("frame").first().reset_index()
    grp = grp.dropna(subset=SOCIAL_FEATURES + ["mean_interanimal_dist"])
    grp["label"] = grp["mean_interanimal_dist"].apply(_label)
    grp = grp[grp["label"].notna() & (grp["label"] != "Single")]

    if len(grp) < 10:
        print("  Insufficient data for confusion matrix — skipping plot 4")
        return

    clf    = joblib.load(CLASSIFIER_PKL)
    X      = grp[SOCIAL_FEATURES].values.astype(np.float32)
    y      = grp["label"].values
    classes = sorted(set(y))

    _, X_te, _, y_te = train_test_split(
        X, y, test_size=0.2, random_state=42,
        stratify=y if len(np.unique(y)) > 1 else None)
    y_pred = clf.predict(X_te)

    cm   = confusion_matrix(y_te, y_pred, labels=classes, normalize="true")
    fig, ax = plt.subplots(figsize=(6, 5))
    disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=classes)
    disp.plot(ax=ax, colorbar=False, cmap="Blues", values_format=".2f")
    ax.set_title("Social behavior classifier — confusion matrix",
                 fontsize=11, fontweight="bold")
    plt.tight_layout()
    fig.savefig(out_path, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load data — warn rather than crash if files missing
    features_df  = pd.DataFrame()
    behavior_df  = pd.DataFrame()
    social_df    = pd.DataFrame()

    if os.path.exists(FEATURES_CSV):
        features_df = pd.read_csv(FEATURES_CSV)
        features_df["timestamp_s"] = pd.to_numeric(features_df["timestamp_s"], errors="coerce")
        print(f"Loaded features: {len(features_df):,} rows")
        # Merge state column from behavior if available
        if os.path.exists(BEHAVIOR_CSV):
            behavior_df = pd.read_csv(BEHAVIOR_CSV)
            features_df = features_df.merge(
                behavior_df[["frame", "mouse_id", "state"]],
                on=["frame", "mouse_id"], how="left")
    else:
        print(f"WARNING: {FEATURES_CSV} not found — plots 1 and 3 will be empty")

    if os.path.exists(BEHAVIOR_CSV):
        behavior_df = pd.read_csv(BEHAVIOR_CSV)
        print(f"Loaded behavior: {len(behavior_df):,} rows")
    else:
        print(f"WARNING: {BEHAVIOR_CSV} not found — plot 3 will be empty")

    if os.path.exists(SOCIAL_CSV):
        social_df = pd.read_csv(SOCIAL_CSV)
        print(f"Loaded social summary: {len(social_df):,} windows")
    else:
        print(f"WARNING: {SOCIAL_CSV} not found — plot 2 will be empty")

    # Keep only the top 5 most frequent mouse IDs — the 5 real mice
    if not features_df.empty:
        top_mice = (features_df.groupby("mouse_id").size()
                    .nlargest(5).index.tolist())
        print(f"\nFiltered to top 5 mice: {top_mice}")
        features_df = features_df[features_df["mouse_id"].isin(top_mice)]
        behavior_df = behavior_df[behavior_df["mouse_id"].isin(top_mice)]

    print("\nGenerating plots ...")
    plot_per_mouse_activity(features_df,
                             f"{OUTPUT_DIR}/per_mouse_activity.png")
    plot_social_timeline(social_df,
                          f"{OUTPUT_DIR}/group_social_timeline.png")
    plot_state_breakdown(behavior_df,
                          f"{OUTPUT_DIR}/per_mouse_state_breakdown.png")
    plot_confusion_matrix(f"{OUTPUT_DIR}/social_classifier_confusion.png")

    print("\nAll plots written to", OUTPUT_DIR)


if __name__ == "__main__":
    main()
