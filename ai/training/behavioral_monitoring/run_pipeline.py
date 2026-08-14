"""
run_pipeline.py
Mouse Behavioral Monitoring System — full-video local runner.

Points directly at mouse_data.avi — no clip extraction.
FRAME_SKIP=2 processes every other frame, halving RAM and time with
minimal accuracy loss for behavioral metrics.

Usage:
    python run_pipeline.py
"""

import matplotlib
matplotlib.use("Agg")

import os
import sys
import time
import subprocess

import psutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import pytesseract

if sys.platform == "win32":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

from behavior_analysis import (
    VIDEO_PATH, OUTPUT_CSV_PATH, SUMMARY_CSV_PATH,
    PLOTS_DIR, FPS, FRAME_WIDTH, FRAME_HEIGHT, ROI_TOP_FRACTION,
    MOTION_THRESHOLD, MIN_CONTOUR_AREA, BLUR_KERNEL,
    SMOOTH_WINDOW, ACTIVITY_SMOOTH_WINDOW,
    INACTIVITY_THRESHOLD_PX, SMOOTH_INACTIVITY_THRESHOLD,
    MIN_INACTIVE_FRAMES, WAKE_THRESHOLD, POSSIBLE_SLEEP_MIN_SECONDS,
    STEREO_WINDOW_FRAMES, STEREO_SPATIAL_RADIUS_PX,
    RHYTHM_BIN_MINUTES, ZONE_NAMES_9, PERIMETER_ZONES, CENTER_ZONE,
    NOCTURNAL_START_HOUR, NOCTURNAL_START_MIN,
    NOCTURNAL_END_HOUR, NOCTURNAL_END_MIN,
    FRAME_SKIP, RECORDING_START,
    extract_test_clip, inspect_video, extract_frame_timestamp,
    track_mouse_motion, smooth_positions, calculate_total_distance,
    detect_inactivity, classify_sleep_wake,
    assign_zones_9, detect_thigmotaxis,
    detect_stereotypy, compute_activity_rhythm,
    export_behavior_summary, export_annotated_video,
)

# ── Configuration ────────────────────────────────────────────────────────────
ACTIVE_VIDEO_PATH         = VIDEO_PATH          # mouse_data.avi — no extraction
ANNOTATED_VIDEO_PATH      = "outputs/annotated_behavior.mp4"
ANNOTATED_VIDEO_FIXED     = "outputs/annotated_behavior_fixed.mp4"
# Annotated video preview length. Full 48h export would take ~15h to encode;
# export a short preview instead. Set None only if you have time to spare.
ANNOTATED_PREVIEW_SECONDS = 300                 # 5-minute preview

# With FRAME_SKIP=2, each row covers 2 video frames, so time-based thresholds
# must be divided by FRAME_SKIP to stay correct in row-count terms.
EFFECTIVE_FPS = FPS / FRAME_SKIP                # 7.5 Hz effective sampling rate

os.makedirs("outputs/plots", exist_ok=True)

# ── Helpers ──────────────────────────────────────────────────────────────────
_proc = psutil.Process(os.getpid())

def _mem():
    return f"{_proc.memory_info().rss / 1024**2:.0f} MB"

def _save(path, dpi=120):
    plt.tight_layout()
    plt.savefig(path, dpi=dpi)
    plt.close()
    print(f"  saved -> {path}")

_t0_global = time.time()

def _step(label):
    elapsed = time.time() - _t0_global
    print(f"\n== {label}  [+{elapsed:.0f}s  {_mem()}]")
    return time.time()

# =============================================================================
_t = _step("1. Video Inspection" + " =" * 30)
# =============================================================================

print(f"  Source : {ACTIVE_VIDEO_PATH}")
print(f"  FRAME_SKIP={FRAME_SKIP}  EFFECTIVE_FPS={EFFECTIVE_FPS}")

video_info = inspect_video(ACTIVE_VIDEO_PATH)
total_frames = video_info["frame_count"]
duration_h   = video_info["duration_seconds"] / 3600
expected_rows = total_frames // FRAME_SKIP
print(f"  Duration: {duration_h:.1f} h  Total frames: {total_frames:,}")
print(f"  Expected tracking rows: ~{expected_rows:,}  (at FRAME_SKIP={FRAME_SKIP})")
print(f"  Step done in {time.time()-_t:.1f}s")

# =============================================================================
_t = _step("2. Sample Frames + Threshold Sweep" + " =" * 15)
# =============================================================================

cap = cv2.VideoCapture(ACTIVE_VIDEO_PATH)
sample_indices = [0, 54000, 108000, 162000, 216000, 270000]  # ~1h apart
fig, axes = plt.subplots(2, 3, figsize=(15, 8))
for ax, idx in zip(axes.flat, sample_indices):
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ret, frame = cap.read()
    if ret:
        roi_y = int(frame.shape[0] * ROI_TOP_FRACTION)
        cv2.line(frame, (0, roi_y), (frame.shape[1], roi_y), (0, 255, 0), 2)
        ax.imshow(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        ax.set_title(f"Frame {idx:,}  (~{idx//54000}h)")
    ax.axis("off")
cap.release()
plt.suptitle("Sample Frames — spaced ~1h apart  (green = ROI boundary)")
_save(f"{PLOTS_DIR}/sample_frames.png")

cap = cv2.VideoCapture(ACTIVE_VIDEO_PATH)
ret, frame0 = cap.read(); cap.release()
gray = cv2.cvtColor(frame0, cv2.COLOR_BGR2GRAY)
h, w = gray.shape
roi_gray = gray[int(h * ROI_TOP_FRACTION):h, :]
fig, axes = plt.subplots(2, 4, figsize=(16, 8))
axes[0, 0].imshow(roi_gray, cmap="gray"); axes[0, 0].set_title("ROI"); axes[0, 0].axis("off")
for i, t in enumerate([40, 50, 60, 70, 80, 90]):
    _, thr = cv2.threshold(roi_gray, t, 255, cv2.THRESH_BINARY_INV)
    r, c = divmod(i + 1, 4)
    axes[r, c].imshow(thr, cmap="gray"); axes[r, c].set_title(f"Thresh {t}"); axes[r, c].axis("off")
plt.suptitle("Static Threshold Sweep")
_save(f"{PLOTS_DIR}/threshold_sweep.png")
print(f"  Step done in {time.time()-_t:.1f}s")

# =============================================================================
_t = _step("3. Motion Detection Diagnostic" + " =" * 19)
# =============================================================================

cap = cv2.VideoCapture(ACTIVE_VIDEO_PATH)
ret1, frame1 = cap.read(); ret2, frame2 = cap.read(); cap.release()
gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
diff  = cv2.absdiff(gray1, gray2)
blur  = cv2.GaussianBlur(diff, BLUR_KERNEL, 0)
_, motion_mask = cv2.threshold(blur, MOTION_THRESHOLD, 255, cv2.THRESH_BINARY)
fig, axes = plt.subplots(1, 3, figsize=(15, 5))
for ax, (img, title) in zip(axes, [(gray1, "Frame 1"), (diff, "Motion Diff"), (motion_mask, f"Threshold={MOTION_THRESHOLD}")]):
    ax.imshow(img, cmap="gray"); ax.set_title(title); ax.axis("off")
_save(f"{PLOTS_DIR}/motion_diagnostic.png")
contours, _ = cv2.findContours(motion_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
if contours:
    lc = max(contours, key=cv2.contourArea)
    x, y, bw, bh = cv2.boundingRect(lc)
    print(f"  Largest contour: area={round(cv2.contourArea(lc),1)}  centre=({x+bw//2}, {y+bh//2})")
print(f"  Step done in {time.time()-_t:.1f}s")

# =============================================================================
_t = _step("4. Contour Tracking — full video" + " =" * 17)
# =============================================================================

print(f"  Processing with FRAME_SKIP={FRAME_SKIP}  (~{expected_rows:,} rows expected)...")

tracking_df = track_mouse_motion(
    video_path       = ACTIVE_VIDEO_PATH,
    roi_start_ratio  = ROI_TOP_FRACTION,
    motion_threshold = MOTION_THRESHOLD,
    min_area         = MIN_CONTOUR_AREA,
    blur_kernel      = BLUR_KERNEL,
    frame_skip       = FRAME_SKIP,
)

print(f"\n  tracking_df: {len(tracking_df):,} rows  {_mem()}")
print(f"  Columns: {list(tracking_df.columns)}")
print(f"\n  First 5 rows:")
print(tracking_df.head().to_string())
print(f"\n  Step done in {time.time()-_t:.1f}s")

# =============================================================================
_t = _step("5. Trajectory + Heatmap" + " =" * 27)
# =============================================================================

def _draw_traj(df, x_col, y_col, title, out_path):
    cap = cv2.VideoCapture(ACTIVE_VIDEO_PATH)
    ret, bg = cap.read(); cap.release()
    img = bg.copy()
    pts = list(zip(df[x_col], df[y_col]))
    for i in range(1, len(pts)):
        cv2.line(img, (int(pts[i-1][0]), int(pts[i-1][1])),
                      (int(pts[i][0]),   int(pts[i][1])), (0, 255, 0), 1)
    for px, py in pts:
        cv2.circle(img, (int(px), int(py)), 1, (255, 0, 0), -1)
    plt.figure(figsize=(10, 6))
    plt.imshow(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    plt.title(title); plt.axis("off")
    _save(out_path)

_draw_traj(tracking_df, "x",        "y",        "Raw Trajectory",      f"{PLOTS_DIR}/trajectory_raw.png")
_draw_traj(tracking_df, "x_smooth", "y_smooth", "Smoothed Trajectory", f"{PLOTS_DIR}/trajectory_smooth.png")

plt.figure(figsize=(8, 6))
plt.hist2d(tracking_df["x_smooth"], tracking_df["y_smooth"], bins=80)
plt.gca().invert_yaxis()
plt.colorbar(label="Position count")
plt.title("Mouse Activity Heatmap — 48h"); plt.xlabel("X (px)"); plt.ylabel("Y (px)")
_save(f"{PLOTS_DIR}/heatmap.png")
print(f"  Step done in {time.time()-_t:.1f}s")

# =============================================================================
_t = _step("6. Behavioural Metrics — Distance + Inactivity" + " =" * 8)
# =============================================================================

total_dist = calculate_total_distance(list(zip(tracking_df["x_smooth"], tracking_df["y_smooth"])))
print(f"  Total movement distance: {round(total_dist):,} px  ({round(total_dist/1000, 1)} kpx)")

# EFFECTIVE_FPS corrects rolling-window and episode thresholds for FRAME_SKIP
tracking_df = detect_inactivity(
    tracking_df,
    threshold_px = INACTIVITY_THRESHOLD_PX,
    min_frames   = int(MIN_INACTIVE_FRAMES / FRAME_SKIP),
    window_size  = max(3, int(ACTIVITY_SMOOTH_WINDOW / FRAME_SKIP)),
)
inactive_pct = tracking_df["is_inactive_smooth"].mean() * 100
print(f"  Inactive (smoothed): {round(inactive_pct, 1)} %   Active: {round(100 - inactive_pct, 1)} %")

plt.figure(figsize=(14, 4))
plt.plot(tracking_df["frame"], tracking_df["movement_smooth"], linewidth=0.3)
plt.axhline(SMOOTH_INACTIVITY_THRESHOLD, linestyle="--", color="red", label=f"Threshold ({SMOOTH_INACTIVITY_THRESHOLD} px)")
plt.title("Smoothed Movement — 48h"); plt.xlabel("Frame"); plt.ylabel("Distance (px)")
plt.legend(); plt.grid(True)
_save(f"{PLOTS_DIR}/movement_smooth.png")

fps_val = video_info["fps"]
episodes, start_frame = [], None
for _, row in tracking_df.iterrows():
    if row["is_inactive_smooth"]:
        if start_frame is None: start_frame = row["frame"]
    else:
        if start_frame is not None:
            dur_frames = row["frame"] - start_frame
            if dur_frames >= MIN_INACTIVE_FRAMES:
                episodes.append({"start": start_frame, "end": row["frame"],
                                  "frames": dur_frames,
                                  "seconds": round(dur_frames / fps_val, 1)})
            start_frame = None
print(f"  Inactivity episodes (>= {MIN_INACTIVE_FRAMES} video frames): {len(episodes)}")
print(f"  Step done in {time.time()-_t:.1f}s  {_mem()}")

# =============================================================================
_t = _step("7. Zone Analysis — 9-Zone Grid + Thigmotaxis" + " =" * 8)
# =============================================================================

tracking_df = assign_zones_9(tracking_df, FRAME_WIDTH, FRAME_HEIGHT)
zone_pct = tracking_df["zone"].value_counts(normalize=True) * 100
print("  Zone distribution (%):")
print(zone_pct.round(1).to_string())

cap = cv2.VideoCapture(ACTIVE_VIDEO_PATH)
ret, bg = cap.read(); cap.release()
bh, bw = bg.shape[:2]
roi_y = int(bh * ROI_TOP_FRACTION); roi_h = bh - roi_y
overlay = bg.copy()
for lx in [bw // 3, 2 * bw // 3]:
    cv2.line(overlay, (lx, roi_y), (lx, bh), (255, 255, 0), 1)
for ly in [roi_y + roi_h // 3, roi_y + 2 * roi_h // 3]:
    cv2.line(overlay, (0, ly), (bw, ly), (255, 255, 0), 1)
cv2.line(overlay, (0, roi_y), (bw, roi_y), (0, 255, 0), 2)
for ri, row_zones in enumerate(ZONE_NAMES_9):
    for ci, name in enumerate(row_zones):
        cv2.putText(overlay, name,
                    (int(ci * bw / 3 + bw / 6) - 35, int(roi_y + ri * roi_h / 3 + roi_h / 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
plt.figure(figsize=(10, 6))
plt.imshow(cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB))
plt.title("3x3 Zone Grid"); plt.axis("off")
_save(f"{PLOTS_DIR}/zone_grid.png")

thigmo = detect_thigmotaxis(tracking_df)
fig, axes = plt.subplots(1, 2, figsize=(12, 4))
zone_pct.sort_index().plot(kind="bar", ax=axes[0])
axes[0].set_title("Time per Zone (%)"); axes[0].set_ylabel("% frames")
axes[0].tick_params(axis="x", rotation=45); axes[0].grid(axis="y")
axes[1].pie([thigmo["perimeter_frames"], thigmo["center_frames"]], labels=["Perimeter", "Center"], autopct="%1.1f%%")
axes[1].set_title(f"Thigmotaxis: {thigmo['thigmotaxis_ratio']}  ({thigmo['thigmotaxis_class']})")
_save(f"{PLOTS_DIR}/zone_analysis.png")
print(f"  Thigmotaxis: ratio={thigmo['thigmotaxis_ratio']}  class={thigmo['thigmotaxis_class']}")
print(f"  Step done in {time.time()-_t:.1f}s  {_mem()}")

# =============================================================================
_t = _step("8. Sleep/Wake Classification" + " =" * 22)
# =============================================================================

tracking_df = classify_sleep_wake(
    tracking_df,
    wake_threshold    = WAKE_THRESHOLD,
    sleep_min_seconds = POSSIBLE_SLEEP_MIN_SECONDS,
    fps               = EFFECTIVE_FPS,   # row-count threshold corrected for FRAME_SKIP
)
state_pct = tracking_df["state"].value_counts(normalize=True) * 100
print("  State distribution (%):")
print(state_pct.round(1).to_string())

fig, axes = plt.subplots(1, 2, figsize=(14, 4))
state_pct.plot(kind="bar", ax=axes[0])
axes[0].set_title("Behavioural State — 48h"); axes[0].set_ylabel("% frames")
axes[0].tick_params(axis="x", rotation=20); axes[0].grid(axis="y")
state_map = {"Wake / Active": 2, "Quiet / Rest": 1, "Possible Sleep": 0}
state_num = tracking_df["state"].map(state_map)
axes[1].fill_between(tracking_df["frame"], state_num, alpha=0.6)
axes[1].set_yticks([0, 1, 2]); axes[1].set_yticklabels(["Possible Sleep", "Quiet / Rest", "Wake / Active"])
axes[1].set_xlabel("Frame"); axes[1].set_title("State Timeline — 48h"); axes[1].grid(True)
_save(f"{PLOTS_DIR}/sleep_wake.png")
print(f"  Step done in {time.time()-_t:.1f}s  {_mem()}")

# =============================================================================
_t = _step("9. Activity Rhythm + OCR Timestamp" + " =" * 16)
# =============================================================================

recording_start_ts = None
cap = cv2.VideoCapture(ACTIVE_VIDEO_PATH)
for idx in [0, 150, 300]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ret, frame = cap.read()
    if not ret: continue
    ts_text = extract_frame_timestamp(frame)
    print(f"  Frame {idx:>4}  OCR result: \"{ts_text}\"")
    if ts_text:
        recording_start_ts = ts_text
        print(f"  OK Full timestamp matched: {recording_start_ts}")
        break
cap.release()

if recording_start_ts is None:
    if RECORDING_START is not None:
        recording_start_ts = RECORDING_START
        print(f"  Using RECORDING_START constant: {recording_start_ts}")
    else:
        print("  FAIL OCR found no timestamp and RECORDING_START is not set — relative axis.")

recording_start_hour = recording_start_minute = None
if recording_start_ts:
    try:
        time_part = recording_start_ts.split(" ")[1]
        h_str, m_str, _ = time_part.split(":")
        recording_start_hour, recording_start_minute = int(h_str), int(m_str)
    except (IndexError, ValueError):
        print("  WARNING: Could not parse HH:MM — falling back to relative axis.")

rhythm_df = compute_activity_rhythm(tracking_df, fps=FPS, bin_minutes=RHYTHM_BIN_MINUTES)
print(f"  Rhythm bins: {len(rhythm_df)}")

fig, ax = plt.subplots(figsize=(16, 4))
if recording_start_hour is not None:
    x_labels = []
    for _, r in rhythm_df.iterrows():
        total_min = recording_start_hour * 60 + recording_start_minute + r["bin_start_minutes"]
        x_labels.append(f"{int(total_min // 60) % 24:02d}:{int(total_min % 60):02d}")
    ax.bar(range(len(rhythm_df)), rhythm_df["active_frames"], color="steelblue", width=1.0, label="Active frames")
    tick_step = max(1, len(x_labels) // 24)
    ax.set_xticks(range(0, len(x_labels), tick_step))
    ax.set_xticklabels(x_labels[::tick_step], rotation=45, ha="right", fontsize=7)
    ax.set_xlabel("Clock time")
    start_total = NOCTURNAL_START_HOUR * 60 + NOCTURNAL_START_MIN
    end_total   = (NOCTURNAL_END_HOUR * 60 + NOCTURNAL_END_MIN) % (24 * 60)
    noc_bins = []
    for i, lbl in enumerate(x_labels):
        h_l, m_l = int(lbl[:2]), int(lbl[3:])
        t = h_l * 60 + m_l
        in_win = (t >= start_total) or (t <= end_total) if start_total > end_total else start_total <= t <= end_total
        if in_win: noc_bins.append(i)
    if noc_bins:
        ax.axvspan(noc_bins[0] - 0.5, noc_bins[-1] + 0.5, color="gold", alpha=0.08)
        ax.axvline(noc_bins[0]  - 0.5, linestyle="--", color="goldenrod", linewidth=1.5,
                   label=f"Target feeding window (protocol)  {NOCTURNAL_START_HOUR:02d}:{NOCTURNAL_START_MIN:02d}-{NOCTURNAL_END_HOUR:02d}:{NOCTURNAL_END_MIN:02d}")
        ax.axvline(noc_bins[-1] + 0.5, linestyle="--", color="goldenrod", linewidth=1.5)
    ax.set_title(f"Activity Rhythm — {RHYTHM_BIN_MINUTES}-min bins  (dashed = target feeding window, protocol reference only)")
else:
    ax.bar(rhythm_df["bin_start_minutes"], rhythm_df["active_frames"],
           width=RHYTHM_BIN_MINUTES * 0.9, color="steelblue", align="edge", label="Active frames")
    ax.set_xlabel("Recording time (minutes)")
    ax.set_title(f"Activity Rhythm — {RHYTHM_BIN_MINUTES}-min bins")
ax.set_ylabel("Active frames per bin"); ax.legend(); ax.grid(axis="y")
_save(f"{PLOTS_DIR}/activity_rhythm.png")
print(f"  Step done in {time.time()-_t:.1f}s  {_mem()}")

# =============================================================================
_t = _step("10. Stereotypy Detection" + " =" * 26)
# =============================================================================

tracking_df = detect_stereotypy(tracking_df, STEREO_WINDOW_FRAMES, STEREO_SPATIAL_RADIUS_PX)
behavior_pct = tracking_df["behavior"].value_counts(normalize=True) * 100
print("  Behavior distribution (%):")
print(behavior_pct.round(1).to_string())

fig, axes = plt.subplots(1, 2, figsize=(16, 4))
is_rep = (tracking_df["behavior"] == "Repetitive").astype(int)
axes[0].fill_between(tracking_df["frame"], is_rep, color="crimson", alpha=0.5, label="Repetitive")
axes[0].set_yticks([0, 1]); axes[0].set_yticklabels(["Normal", "Repetitive"])
axes[0].set_xlabel("Frame"); axes[0].set_title(f"Stereotypy Timeline — 48h")
axes[0].grid(True); axes[0].legend()
rep_df = tracking_df[tracking_df["behavior"] == "Repetitive"]
axes[1].scatter(tracking_df["x_smooth"], tracking_df["y_smooth"], c="lightgrey", s=0.3, label="Normal")
if len(rep_df):
    axes[1].scatter(rep_df["x_smooth"], rep_df["y_smooth"], c="crimson", s=0.5, label="Repetitive")
axes[1].invert_yaxis(); axes[1].set_title("Repetitive Frame Locations")
axes[1].set_xlabel("X (px)"); axes[1].set_ylabel("Y (px)"); axes[1].legend(markerscale=6)
_save(f"{PLOTS_DIR}/stereotypy.png")
print(f"  Step done in {time.time()-_t:.1f}s  {_mem()}")

# =============================================================================
_t = _step("11. Annotated Video Preview" + " =" * 24)
# =============================================================================

preview_frames = int(ANNOTATED_PREVIEW_SECONDS * FPS) if ANNOTATED_PREVIEW_SECONDS else None
if preview_frames:
    print(f"  Exporting {ANNOTATED_PREVIEW_SECONDS}s preview ({preview_frames} frames) — "
          f"set ANNOTATED_PREVIEW_SECONDS=None for full 48h (not recommended: ~15h encode time).")
else:
    print("  WARNING: Exporting full 48h video — this will take many hours.")

export_annotated_video(ACTIVE_VIDEO_PATH, tracking_df, ANNOTATED_VIDEO_PATH,
                       max_frames=preview_frames)

print("Re-encoding to H.264...")
subprocess.run([
    "ffmpeg", "-y", "-i", ANNOTATED_VIDEO_PATH,
    "-vcodec", "libx264", "-pix_fmt", "yuv420p", ANNOTATED_VIDEO_FIXED,
], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
size_mb = round(os.path.getsize(ANNOTATED_VIDEO_FIXED) / (1024**2), 1)
print(f"  Preview video: {ANNOTATED_VIDEO_FIXED}  ({size_mb} MB)")
print(f"  Step done in {time.time()-_t:.1f}s  {_mem()}")

# =============================================================================
_t = _step("12. CSV Export" + " =" * 35)
# =============================================================================

os.makedirs("outputs", exist_ok=True)
tracking_df.to_csv(OUTPUT_CSV_PATH, index=False)
csv_kb = os.path.getsize(OUTPUT_CSV_PATH) // 1024
print(f"  tracking_data.csv : {OUTPUT_CSV_PATH}  ({len(tracking_df):,} rows  {csv_kb} KB)")
print(f"  Columns: {list(tracking_df.columns)}")

summary_df = export_behavior_summary(tracking_df, fps=FPS, output_path=SUMMARY_CSV_PATH)

# =============================================================================
elapsed_total = time.time() - _t0_global
print("\n" + "=" * 60)
print("PIPELINE COMPLETE")
print("=" * 60)
print(f"  Total runtime      : {elapsed_total/60:.1f} min")
print(f"  Peak memory (approx): {_mem()}")
print(f"  tracking_data.csv  : {os.path.getsize(OUTPUT_CSV_PATH)//1024} KB  ({len(tracking_df):,} rows)")
print(f"  behavior_summary   : {os.path.getsize(SUMMARY_CSV_PATH)//1024} KB  ({len(summary_df)} rows)")
print(f"  annotated preview  : {round(os.path.getsize(ANNOTATED_VIDEO_FIXED)/(1024**2),1)} MB  ({ANNOTATED_PREVIEW_SECONDS}s)")
print(f"  plots              : {PLOTS_DIR}/")
for p in sorted(os.listdir(PLOTS_DIR)):
    print(f"    {p}")
print("=" * 60)
