"""
behavior_analysis.py
Mouse Behavioral Monitoring System — the industry partner Integrated IVC Cage Project

Importable module: all pipeline steps exposed as clean functions.
All parameters are named constants at the top. No magic numbers in logic.
"""

import os
import re
import math
import subprocess
from collections import deque

import cv2
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

# ============================================================
# CONSTANTS — all tunable values live here, nowhere else
# ============================================================

# Paths
VIDEO_PATH               = "mouse_data.avi"
CLIP_DURATION_SECONDS    = 600
OUTPUT_CSV_PATH          = "outputs/tracking_data.csv"
SUMMARY_CSV_PATH         = "outputs/behavior_summary.csv"
PLOTS_DIR                = "outputs/plots"

# Camera / cage geometry
FPS                      = 15
FRAME_WIDTH              = 640
FRAME_HEIGHT             = 480
CAGE_WIDTH_MM            = 205   # IVC cage spec — reserved for future px→mm calibration
ROI_TOP_FRACTION         = 0.45  # top 45 % cropped: hardware, timestamp overlay, reflections

# Motion detection pipeline (do not change — validated in notebook)
MOTION_THRESHOLD         = 15
MIN_CONTOUR_AREA         = 20
BLUR_KERNEL              = (11, 11)

# Trajectory smoothing
SMOOTH_WINDOW            = 5
ACTIVITY_SMOOTH_WINDOW   = 15

# Inactivity / sleep classification
INACTIVITY_THRESHOLD_PX      = 2.0
SMOOTH_INACTIVITY_THRESHOLD  = 2.5
MIN_INACTIVE_FRAMES          = 30
WAKE_THRESHOLD               = 2.5
POSSIBLE_SLEEP_MIN_SECONDS   = 30

# Stereotypy detection
STEREO_WINDOW_FRAMES     = 45   # 3 s at 15 FPS
STEREO_SPATIAL_RADIUS_PX = 10

# Activity rhythm
RHYTHM_BIN_MINUTES       = 10

# Frame sampling — set to 2 to process every other frame (halves RAM and time)
FRAME_SKIP               = 2

# Nocturnal feeding window (used for plot annotation)
NOCTURNAL_START_HOUR     = 23
NOCTURNAL_START_MIN      = 0
NOCTURNAL_END_HOUR       = 0
NOCTURNAL_END_MIN        = 30

# OCR timestamp fallback — set to "YYYY HH:MM:SS" if OCR cannot read the video
RECORDING_START          = "2023 15:17:30"

# ============================================================
# ZONE CONFIGURATION — 3×3 grid over ROI
# ============================================================

ZONE_NAMES_9 = [
    ["TopLeft",  "TopCenter",  "TopRight"],
    ["MidLeft",  "Center",     "MidRight"],
    ["BotLeft",  "BotCenter",  "BotRight"],
]

PERIMETER_ZONES = {
    "TopLeft", "TopCenter", "TopRight",
    "MidLeft",              "MidRight",
    "BotLeft", "BotCenter", "BotRight",
}

CENTER_ZONE = "Center"

# ============================================================
# VIDEO UTILITIES
# ============================================================

def extract_test_clip(source_path, output_path, duration_seconds=CLIP_DURATION_SECONDS):
    """Extract a re-encoded MP4 test clip from the source AVI via FFmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", source_path,
        "-t", str(duration_seconds),
        "-vcodec", "libx264",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    subprocess.run(cmd, check=True)
    return output_path


def inspect_video(video_path):
    """Return basic video metadata as a dict."""
    print("File exists:", os.path.exists(video_path))
    if os.path.exists(video_path):
        print("File size MB:", round(os.path.getsize(video_path) / (1024 ** 2), 2))

    cap = cv2.VideoCapture(video_path)
    frame_count    = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps            = cap.get(cv2.CAP_PROP_FPS)
    width          = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height         = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration_s     = frame_count / fps if fps > 0 else 0
    cap.release()

    print(f"Frames: {frame_count}  FPS: {fps}  Resolution: {width}x{height}  Duration: {round(duration_s, 1)} s")
    return {"frame_count": frame_count, "fps": fps, "width": width, "height": height, "duration_seconds": duration_s}


_TIMESTAMP_RE = re.compile(r"\d{3,4} \d{2}:\d{2}:\d{2}")


def extract_frame_timestamp(frame):
    """Read the burned-in clock timestamp from the top-left of an uncropped frame.

    The timestamp sits in the top ROI_TOP_FRACTION of the frame — the same region
    that gets cropped out before motion detection. Read this from the *original*
    frame before applying ROI.

    Returns 'YYYY HH:MM:SS' if the exact pattern is found in the OCR output,
    otherwise '' — no partial matches are returned.  Callers should fall back to
    the RECORDING_START constant when this returns ''.
    Only HH:MM:SS is used downstream; the year is matched to anchor the regex
    but is discarded by the caller via split(" ")[1].
    """
    if not TESSERACT_AVAILABLE:
        return ""
    h, w = frame.shape[:2]
    roi_end_y  = int(h * ROI_TOP_FRACTION)
    roi_end_x  = w // 2  # timestamp is in the left half
    region     = frame[0:roi_end_y, 0:roi_end_x]
    gray       = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    # Upscale for better OCR accuracy
    enlarged   = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, binary  = cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # PSM 11 (sparse text) works best for a small timestamp on a cluttered background.
    # Whitelist is intentionally omitted — it collapses spaces and hurts recognition here.
    config = r"--psm 11"
    try:
        text = pytesseract.image_to_string(binary, config=config).strip()
    except Exception:
        return ""
    match = _TIMESTAMP_RE.search(text)
    return match.group() if match else ""


# ============================================================
# CORE TRACKING PIPELINE
# ============================================================

def smooth_positions(positions, window_size=SMOOTH_WINDOW):
    """Apply a symmetric moving-average to a list of (x, y) tuples."""
    smoothed = []
    n = len(positions)
    for i in range(n):
        start = max(0, i - window_size)
        end   = min(n, i + window_size + 1)
        xs    = [p[0] for p in positions[start:end]]
        ys    = [p[1] for p in positions[start:end]]
        smoothed.append((int(np.mean(xs)), int(np.mean(ys))))
    return smoothed


def calculate_total_distance(positions):
    """Return total Euclidean distance (pixels) along a trajectory."""
    total = 0.0
    for i in range(1, len(positions)):
        x1, y1 = positions[i - 1]
        x2, y2 = positions[i]
        total += math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    return total


def track_mouse_motion(
    video_path,
    roi_start_ratio  = ROI_TOP_FRACTION,
    motion_threshold = MOTION_THRESHOLD,
    min_area         = MIN_CONTOUR_AREA,
    blur_kernel      = BLUR_KERNEL,
    frame_skip       = FRAME_SKIP,
):
    """Run the validated frame-diff tracking pipeline.

    Pipeline (do not modify): frame diff → Gaussian blur → binary threshold →
    contour detection → largest contour centroid.

    frame_skip: decode every Nth frame; intermediate frames are grabbed without
    decoding (cap.grab()) for speed.  frame numbers in the returned DataFrame
    reflect actual positions in the source video.

    Returns a tracking_df with columns:
        frame, x, y, motion_area, x_smooth, y_smooth, distance_from_previous_px
    """
    cap = cv2.VideoCapture(video_path)
    positions, frame_numbers, areas = [], [], []

    ret, prev_frame = cap.read()
    if not ret:
        cap.release()
        raise ValueError("Could not read first frame from video.")

    prev_gray   = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
    frame_index = 1

    while True:
        # Grab (seek without decoding) skipped frames — much faster than cap.read()
        for _ in range(frame_skip - 1):
            if not cap.grab():
                break

        ret, frame = cap.read()
        if not ret:
            break

        gray        = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w        = gray.shape
        roi_y_start = int(h * roi_start_ratio)

        roi_prev = prev_gray[roi_y_start:h, :]
        roi_curr = gray[roi_y_start:h, :]

        diff  = cv2.absdiff(roi_prev, roi_curr)
        blur  = cv2.GaussianBlur(diff, blur_kernel, 0)
        _, thresh = cv2.threshold(blur, motion_threshold, 255, cv2.THRESH_BINARY)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if contours:
            largest = max(contours, key=cv2.contourArea)
            area    = cv2.contourArea(largest)
            if area > min_area:
                x, y, bw, bh = cv2.boundingRect(largest)
                cx = x + bw // 2
                cy = roi_y_start + y + bh // 2
                positions.append((cx, cy))
                frame_numbers.append(frame_index)
                areas.append(area)

        prev_gray    = gray.copy()
        frame_index += frame_skip

    cap.release()

    df = pd.DataFrame({
        "frame":      frame_numbers,
        "x":          [p[0] for p in positions],
        "y":          [p[1] for p in positions],
        "motion_area": areas,
    })

    smoothed             = smooth_positions(positions, window_size=SMOOTH_WINDOW)
    df["x_smooth"]       = [p[0] for p in smoothed]
    df["y_smooth"]       = [p[1] for p in smoothed]

    df["distance_from_previous_px"] = 0.0
    for i in range(1, len(df)):
        x1, y1 = df.at[i - 1, "x_smooth"], df.at[i - 1, "y_smooth"]
        x2, y2 = df.at[i,     "x_smooth"], df.at[i,     "y_smooth"]
        df.at[i, "distance_from_previous_px"] = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)

    print(f"Tracked {len(df)} positions across {frame_index} frames.")
    return df


# ============================================================
# BEHAVIORAL FEATURE EXTRACTION
# ============================================================

def detect_inactivity(
    tracking_df,
    threshold_px = INACTIVITY_THRESHOLD_PX,
    min_frames   = MIN_INACTIVE_FRAMES,
    window_size  = ACTIVITY_SMOOTH_WINDOW,
):
    """Add smoothed movement and inactivity flag columns.

    Adds:
        is_inactive       — hard per-frame threshold on raw distance
        movement_smooth   — rolling mean of distance_from_previous_px
        is_inactive_smooth — soft threshold applied to movement_smooth

    Returns the modified tracking_df.
    """
    df = tracking_df.copy()
    df["is_inactive"] = df["distance_from_previous_px"] < threshold_px

    df["movement_smooth"] = (
        df["distance_from_previous_px"]
        .rolling(window_size, center=True, min_periods=1)
        .mean()
        .fillna(0)
    )
    df["is_inactive_smooth"] = df["movement_smooth"] < SMOOTH_INACTIVITY_THRESHOLD
    return df


def classify_sleep_wake(
    tracking_df,
    wake_threshold      = WAKE_THRESHOLD,
    sleep_min_seconds   = POSSIBLE_SLEEP_MIN_SECONDS,
    fps                 = FPS,
):
    """Classify each frame into a sleep/wake state.

    Requires detect_inactivity() to have been called first (needs movement_smooth).

    States:
        'Wake / Active'   — movement above wake_threshold
        'Quiet / Rest'    — below threshold, short duration
        'Possible Sleep'  — below threshold for >= sleep_min_seconds

    Adds columns: state, possible_sleep
    """
    df = tracking_df.copy()
    df["state"] = "Wake / Active"
    df.loc[df["movement_smooth"] < wake_threshold, "state"] = "Quiet / Rest"
    df["possible_sleep"] = False

    min_sleep_frames = int(sleep_min_seconds * fps)
    start_idx = None

    for idx, row in df.iterrows():
        if row["state"] == "Quiet / Rest":
            if start_idx is None:
                start_idx = idx
        else:
            if start_idx is not None:
                if (idx - start_idx) >= min_sleep_frames:
                    df.loc[start_idx:idx - 1, "state"]          = "Possible Sleep"
                    df.loc[start_idx:idx - 1, "possible_sleep"] = True
                start_idx = None

    # Handle rest period extending to end of recording
    if start_idx is not None:
        end_idx = df.index[-1]
        if (end_idx - start_idx) >= min_sleep_frames:
            df.loc[start_idx:end_idx, "state"]          = "Possible Sleep"
            df.loc[start_idx:end_idx, "possible_sleep"] = True

    return df


def assign_zones_9(tracking_df, frame_width=FRAME_WIDTH, frame_height=FRAME_HEIGHT):
    """Assign a 3×3 grid zone label to each tracked frame.

    Zones (row × col):
        TopLeft  TopCenter  TopRight
        MidLeft  Center     MidRight
        BotLeft  BotCenter  BotRight

    The grid covers the active ROI (lower 1 - ROI_TOP_FRACTION of the frame).
    Perimeter zones are used for thigmotaxis detection.
    Adds column: zone
    """
    df             = tracking_df.copy()
    roi_y_start    = int(frame_height * ROI_TOP_FRACTION)
    roi_height     = frame_height - roi_y_start

    x_thirds = [frame_width / 3, 2 * frame_width / 3]
    y_thirds = [
        roi_y_start + roi_height / 3,
        roi_y_start + 2 * roi_height / 3,
    ]

    def _zone(x, y):
        col = 0 if x < x_thirds[0] else (1 if x < x_thirds[1] else 2)
        row = 0 if y < y_thirds[0] else (1 if y < y_thirds[1] else 2)
        return ZONE_NAMES_9[row][col]

    df["zone"] = df.apply(lambda r: _zone(r["x_smooth"], r["y_smooth"]), axis=1)
    return df


def detect_thigmotaxis(tracking_df):
    """Compute thigmotaxis metrics from the zone column.

    Thigmotaxis = sustained preference for perimeter zones over the center,
    used as a proxy for anxiety.

    Returns a summary dict:
        perimeter_frames, center_frames, total_frames,
        thigmotaxis_ratio, thigmotaxis_class ('High' / 'Normal' / 'Low')
    """
    if "zone" not in tracking_df.columns:
        raise ValueError("assign_zones_9() must be called before detect_thigmotaxis().")

    zones     = tracking_df["zone"]
    total     = len(zones)
    perimeter = int(zones.isin(PERIMETER_ZONES).sum())
    center    = int((zones == CENTER_ZONE).sum())
    ratio     = perimeter / total if total > 0 else 0.0

    if ratio >= 0.70:
        classification = "High"
    elif ratio <= 0.40:
        classification = "Low"
    else:
        classification = "Normal"

    return {
        "perimeter_frames":   perimeter,
        "center_frames":      center,
        "total_frames":       total,
        "thigmotaxis_ratio":  round(ratio, 4),
        "thigmotaxis_class":  classification,
    }


def detect_stereotypy(
    tracking_df,
    window_frames    = STEREO_WINDOW_FRAMES,
    spatial_radius_px = STEREO_SPATIAL_RADIUS_PX,
):
    """Detect repetitive movement patterns as a stereotypy/anxiety proxy.

    Method: for each frame, compute the spatial spread (combined std of x and y)
    over a rolling window. A small spread while the mouse is still moving indicates
    back-and-forth motion in a confined area — the hallmark of stereotypy.

    Gate: frames classified as 'Possible Sleep' are always Normal — a stationary
    mouse in a sleep bout has near-zero spatial spread by definition, which would
    otherwise cause every sleep period to be misclassified as Repetitive.

    Requires detect_inactivity() (needs movement_smooth) and classify_sleep_wake()
    (needs state) to have been called first.
    Adds column: behavior  ('Normal' | 'Repetitive')
    """
    df    = tracking_df.copy()
    x_std = df["x_smooth"].rolling(window_frames, center=True, min_periods=1).std().fillna(0)
    y_std = df["y_smooth"].rolling(window_frames, center=True, min_periods=1).std().fillna(0)
    spatial_spread = np.sqrt(x_std ** 2 + y_std ** 2)

    is_moving = (
        df["movement_smooth"] >= WAKE_THRESHOLD
        if "movement_smooth" in df.columns
        else pd.Series(True, index=df.index)
    )
    not_sleeping = (
        df["state"] != "Possible Sleep"
        if "state" in df.columns
        else pd.Series(True, index=df.index)
    )

    df["behavior"] = "Normal"
    df.loc[(spatial_spread < spatial_radius_px) & is_moving & not_sleeping, "behavior"] = "Repetitive"
    return df


def compute_activity_rhythm(tracking_df, fps=FPS, bin_minutes=RHYTHM_BIN_MINUTES):
    """Bin movement data into time windows to reveal circadian activity patterns.

    Requires detect_inactivity() (needs movement_smooth).

    Returns rhythm_df with columns:
        bin_start_frame, bin_end_frame,
        bin_start_minutes, bin_end_minutes,
        active_frames, total_frames_in_bin, activity_ratio
    """
    if "movement_smooth" not in tracking_df.columns:
        raise ValueError("detect_inactivity() must be called before compute_activity_rhythm().")

    bin_frames   = int(bin_minutes * 60 * fps)
    max_frame    = int(tracking_df["frame"].max())
    rows         = []

    for bin_start in range(0, max_frame + 1, bin_frames):
        bin_end = bin_start + bin_frames
        mask    = (tracking_df["frame"] >= bin_start) & (tracking_df["frame"] < bin_end)
        window  = tracking_df[mask]
        if len(window) == 0:
            continue
        active_n = int((window["movement_smooth"] >= WAKE_THRESHOLD).sum())
        rows.append({
            "bin_start_frame":    bin_start,
            "bin_end_frame":      bin_end,
            "bin_start_minutes":  round(bin_start / fps / 60, 1),
            "bin_end_minutes":    round(bin_end   / fps / 60, 1),
            "active_frames":      active_n,
            "total_frames_in_bin": len(window),
            "activity_ratio":     round(active_n / len(window), 4),
        })

    return pd.DataFrame(rows)


# ============================================================
# OUTPUT EXPORTERS
# ============================================================

def export_behavior_summary(tracking_df, fps=FPS, output_path=SUMMARY_CSV_PATH):
    """Export a per-bin behavioral summary CSV.

    One row per RHYTHM_BIN_MINUTES window, columns:
        window_start_frame, window_end_frame, dominant_state,
        total_distance_px, active_frames, inactive_frames,
        dominant_zone, thigmotaxis_ratio, stereotypy_flag, behavior_notes

    Returns the summary DataFrame.
    """
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    bin_frames  = int(RHYTHM_BIN_MINUTES * 60 * fps)
    max_frame   = int(tracking_df["frame"].max())
    rows        = []

    for bin_start in range(0, max_frame + 1, bin_frames):
        bin_end = bin_start + bin_frames
        mask    = (tracking_df["frame"] >= bin_start) & (tracking_df["frame"] < bin_end)
        window  = tracking_df[mask]
        if len(window) == 0:
            continue

        dominant_state = (
            window["state"].mode()[0] if "state" in window.columns and len(window) > 0
            else ""
        )
        total_distance = (
            round(float(window["distance_from_previous_px"].sum()), 2)
            if "distance_from_previous_px" in window.columns else 0.0
        )
        if "movement_smooth" in window.columns:
            active_frames   = int((window["movement_smooth"] >= WAKE_THRESHOLD).sum())
            inactive_frames = len(window) - active_frames
        else:
            active_frames   = 0
            inactive_frames = len(window)

        dominant_zone = (
            window["zone"].mode()[0] if "zone" in window.columns and len(window) > 0
            else ""
        )
        thigmo_ratio = 0.0
        if "zone" in window.columns and len(window) > 0:
            thigmo_ratio = detect_thigmotaxis(window)["thigmotaxis_ratio"]

        stereotypy_flag = False
        if "behavior" in window.columns and len(window) > 0:
            stereotypy_flag = bool((window["behavior"] == "Repetitive").mean() > 0.10)

        rows.append({
            "window_start_frame": bin_start,
            "window_end_frame":   bin_end,
            "dominant_state":     dominant_state,
            "total_distance_px":  total_distance,
            "active_frames":      active_frames,
            "inactive_frames":    inactive_frames,
            "dominant_zone":      dominant_zone,
            "thigmotaxis_ratio":  thigmo_ratio,
            "stereotypy_flag":    stereotypy_flag,
            "behavior_notes":     "",
        })

    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(output_path, index=False)
    print(f"Saved behavior summary: {output_path}  ({len(summary_df)} rows)")
    return summary_df


def export_annotated_video(video_path, tracking_df, output_path, max_frames=None):
    """Re-run motion detection and overlay behavioral annotations.

    Overlays per-frame from tracking_df:
        - bounding box + centroid (green/red)
        - trajectory tail (blue, last 50 positions)
        - state, zone, behavior labels (bottom of frame)
        - frame counter (top-left)

    max_frames: stop after writing this many frames (None = full video).
    For a 48-hour source, pass max_frames=int(FPS*seconds) to export a preview.

    The raw output uses mp4v codec; re-encode with FFmpeg if browser
    playback fails (see notebook cell below export call).

    Returns output_path.
    """
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    cap    = cv2.VideoCapture(video_path)
    fps    = int(cap.get(cv2.CAP_PROP_FPS))
    width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out    = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    frame_lookup = tracking_df.set_index("frame")
    ret, prev_frame = cap.read()
    if not ret:
        cap.release()
        raise ValueError("Could not read first frame for annotated export.")

    prev_gray   = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
    trajectory  = deque(maxlen=50)
    frame_index = 1

    frames_written = 0
    while True:
        if max_frames is not None and frames_written >= max_frames:
            break

        ret, frame = cap.read()
        if not ret:
            break

        gray        = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w        = gray.shape
        roi_y_start = int(h * ROI_TOP_FRACTION)

        roi_prev = prev_gray[roi_y_start:h, :]
        roi_curr = gray[roi_y_start:h, :]

        diff  = cv2.absdiff(roi_prev, roi_curr)
        blur  = cv2.GaussianBlur(diff, BLUR_KERNEL, 0)
        _, thresh = cv2.threshold(blur, MOTION_THRESHOLD, 255, cv2.THRESH_BINARY)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest = max(contours, key=cv2.contourArea)
            area    = cv2.contourArea(largest)
            if area > MIN_CONTOUR_AREA:
                x, y, bw, bh = cv2.boundingRect(largest)
                y_global     = y + roi_y_start
                cx           = x + bw // 2
                cy           = y_global + bh // 2
                trajectory.append((cx, cy))
                cv2.rectangle(frame, (x, y_global), (x + bw, y_global + bh), (0, 255, 0), 2)
                cv2.circle(frame, (cx, cy), 5, (0, 0, 255), -1)

        # Trajectory tail
        for i in range(1, len(trajectory)):
            cv2.line(frame, trajectory[i - 1], trajectory[i], (255, 0, 0), 2)

        # Behavioral annotation overlay from tracking_df
        if frame_index in frame_lookup.index:
            row          = frame_lookup.loc[frame_index]
            state_lbl    = row.get("state",    "") if hasattr(row, "get") else str(row.get("state",    ""))
            zone_lbl     = row.get("zone",     "") if hasattr(row, "get") else str(row.get("zone",     ""))
            behavior_lbl = row.get("behavior", "") if hasattr(row, "get") else str(row.get("behavior", ""))
            cv2.putText(frame, f"State: {state_lbl}",    (10, height - 60), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            cv2.putText(frame, f"Zone: {zone_lbl}",      (10, height - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            cv2.putText(frame, f"Behavior: {behavior_lbl}", (10, height - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (100, 100, 255), 1)

        cv2.putText(frame, f"Frame: {frame_index}", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)

        out.write(frame)
        frames_written += 1
        prev_gray   = gray.copy()
        frame_index += 1

    cap.release()
    out.release()
    print(f"Saved annotated video: {output_path}")
    return output_path
