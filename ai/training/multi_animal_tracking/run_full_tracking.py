"""
run_full_tracking.py
One-shot script — run run_multi_tracking() on the full multiple_mouse.mp4,
save multi_tracking.csv and group_summary.csv, print summary stats.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from multi_track import (
    run_multi_tracking, export_group_summary, compute_group_rhythm,
    inspect_video,
    VIDEO_PATH, OUTPUT_CSV_PATH, SUMMARY_CSV_PATH,
    FPS, NUM_MICE, FRAME_SKIP,
)

os.makedirs("outputs", exist_ok=True)

print("=" * 60)
print("Multi-Animal Tracking -- Full Video Run")
print("=" * 60)

info = inspect_video(VIDEO_PATH)
print(f"\nSource : {VIDEO_PATH}")
print(f"  {info['frame_count']:,} frames  |  {info['fps']} FPS  |  "
      f"{info['width']}x{info['height']}  |  {info['duration_h']:.3f} h")
print(f"  FRAME_SKIP={FRAME_SKIP}  ->  ~{info['frame_count'] // FRAME_SKIP:,} decoded frames to process")

t_wall = time.time()
df = run_multi_tracking(
    video_path     = VIDEO_PATH,
    frame_skip     = FRAME_SKIP,
    progress_every = 50_000,
)

print(f"\nSaving {OUTPUT_CSV_PATH} ...")
df.to_csv(OUTPUT_CSV_PATH, index=False)
csv_mb = os.path.getsize(OUTPUT_CSV_PATH) / (1024 ** 2)
print(f"  Saved  {len(df):,} rows  ({csv_mb:.1f} MB)")

print(f"\nComputing group rhythm ...")
rhythm_df = compute_group_rhythm(df, fps=FPS)
rhythm_df.to_csv(SUMMARY_CSV_PATH, index=False)
print(f"  group_summary.csv  {len(rhythm_df)} bins  ({SUMMARY_CSV_PATH})")

total_elapsed = time.time() - t_wall
em, es = divmod(int(total_elapsed), 60)

print("\n" + "=" * 60)
print("RESULTS")
print("=" * 60)
print(f"  Total tracked rows     : {len(df):,}")
print(f"  Mean active bodies     : {df['active_bodies'].mean():.3f} / {NUM_MICE}")
print(f"  Mean motion energy     : {df['motion_energy'].mean():.6f}")
print(f"  active_bodies range    : {df['active_bodies'].min()} - {df['active_bodies'].max()}")
print(f"  group_summary bins     : {len(rhythm_df)}")
print(f"  timestamp_clock range  : {df['timestamp_clock'].iloc[0]}  ->  {df['timestamp_clock'].iloc[-1]}")
print(f"  Total wall time        : {em}m {es:02d}s")

print(f"\n  Group state distribution:")
sc  = df["group_state"].value_counts()
pct = df["group_state"].value_counts(normalize=True) * 100
for state in ["All Active", "Mostly Active", "Mostly Resting", "All Resting"]:
    c = sc.get(state, 0)
    p = pct.get(state, 0.0)
    bar = "#" * int(p / 2)
    print(f"    {state:<18} {p:5.1f}%  ({c:>7,} frames)  {bar}")

print(f"\n  Zone distribution:")
zc = df["dominant_zone"].value_counts(normalize=True) * 100
for z in ["Left", "Center", "Right"]:
    print(f"    {z:<10} {zc.get(z, 0.0):5.1f}%")

print(f"\n  First 5 rows of group_summary.csv:")
print(rhythm_df.head(5).to_string(index=False))

print("\n" + "=" * 60)
print("Full run complete.")
print("=" * 60)
