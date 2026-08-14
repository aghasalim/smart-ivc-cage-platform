"""
multi_track.py
Group-level motion tracking for 5 mice in the the industry partner IVC cage.
Front-facing wide-angle camera, 9.78h recording, 25 FPS.

No individual identity tracking — Option A (group-level counts only).
Do NOT import from or modify behavioral_monitoring/.

Run via run_multi_track.ipynb or as a module:
    from multi_track import run_multi_tracking, ...
"""

import os
import re
import subprocess
import platform
import time
import logging
import logging.handlers

import cv2
import numpy as np
import pandas as pd

# ── Tesseract (optional) ──────────────────────────────────────────────────────
try:
    import pytesseract
    if platform.system() == "Windows":
        pytesseract.pytesseract.tesseract_cmd = (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
    _TESSERACT_OK = True
except ImportError:
    _TESSERACT_OK = False

# Configure logging
LOG_PATH = "outputs/multi_track.log"
os.makedirs("outputs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=10_000_000, backupCount=3
        ),
        logging.StreamHandler()   # still prints to terminal
    ]
)
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
VIDEO_PATH            = "multiple_mouse.mp4"
TEST_CLIP_PATH        = "test_clip.mp4"
CLIP_DURATION_SECONDS = 300
OUTPUT_CSV_PATH       = "outputs/multi_tracking.csv"
SUMMARY_CSV_PATH      = "outputs/group_summary.csv"

# ── Camera / cage ─────────────────────────────────────────────────────────────
FPS              = 25.0
NUM_MICE         = 5
ROI_TOP_FRACTION = 0.55    # discard top 55% — front-facing camera (noise, hardware)

# ── Motion detection ──────────────────────────────────────────────────────────
MOTION_THRESHOLD = 20      # slightly higher than single-mouse (busier background)
MIN_CONTOUR_AREA = 300     # front-facing bodies appear larger than top-down
BLUR_KERNEL      = (11, 11)

# ── Frame processing ──────────────────────────────────────────────────────────
FRAME_SKIP         = 2
SMOOTH_WINDOW      = 5
RHYTHM_BIN_MINUTES = 10

# ── Group state thresholds ────────────────────────────────────────────────────
ALL_ACTIVE_MIN    = 4    # >= 4 bodies → "All Active"
MOSTLY_ACTIVE_MIN = 2    # 2–3 bodies  → "Mostly Active"
                         #   1 body    → "Mostly Resting"
                         #   0 bodies  → "All Resting"

# ── Social proximity thresholds ───────────────────────────────────────────────
HUDDLE_THRESHOLD_PX  = 80    # mean inter-animal dist below this = huddling
EXPLORE_THRESHOLD_PX = 200   # mean inter-animal dist above this = exploration

# ── OCR ───────────────────────────────────────────────────────────────────────
RECORDING_START = "00:46:35"    # HH:MM:SS fallback — confirmed from frame-0 OCR ("2023 00:46:35")
_TIMESTAMP_RE   = re.compile(r"\d{3,4} \d{2}:\d{2}:\d{2}")


# ─────────────────────────────────────────────────────────────────────────────
# Video utilities
# ─────────────────────────────────────────────────────────────────────────────

def inspect_video(video_path: str) -> dict:
    """Return metadata dict for video_path."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {video_path}")
    info = {
        "path":        video_path,
        "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        "fps":         cap.get(cv2.CAP_PROP_FPS),
        "width":       int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height":      int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
    cap.release()
    info["duration_s"]  = round(info["frame_count"] / info["fps"], 1) if info["fps"] else 0
    info["duration_h"]  = round(info["duration_s"] / 3600, 3)
    info["roi_top_px"]  = int(info["height"] * ROI_TOP_FRACTION)
    info["roi_area_px"] = info["width"] * (info["height"] - info["roi_top_px"])
    return info


def extract_frame_timestamp(frame: np.ndarray) -> str:
    """
    OCR the timestamp from the top-left corner of a raw video frame.
    Returns 'HH:MM:SS'; empty string on failure.
    Same regex / PSM strategy as behavioral_monitoring (partial-year format).
    """
    if not _TESSERACT_OK:
        return ""
    h, w = frame.shape[:2]
    crop = frame[:int(h * 0.12), :int(w * 0.30)]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(binary, config="--psm 11")
    m = _TIMESTAMP_RE.search(text)
    if m:
        return m.group(0).split(" ", 1)[-1]    # return HH:MM:SS only
    return ""


def extract_test_clip(source: str, output: str, duration: int) -> None:
    """Extract first `duration` seconds from source via FFmpeg (H.264)."""
    parent = os.path.dirname(output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-i", source,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        output,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    size_mb = os.path.getsize(output) / (1024 ** 2)
    logger.info(f"Clip extracted: {output}  ({duration}s, {size_mb:.1f} MB)")


# ─────────────────────────────────────────────────────────────────────────────
# Per-frame detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def detect_all_contours(
    frame:      np.ndarray,
    prev_frame: np.ndarray,
    roi_top:    int,
    threshold:  int = MOTION_THRESHOLD,
    min_area:   int = MIN_CONTOUR_AREA,
) -> list:
    """
    Detect ALL moving contours in the ROI (bottom 45% of frame).
    Pipeline: crop → absdiff → GaussianBlur → threshold → findContours.
    Returns contours shifted to full-frame coordinates (y += roi_top).
    """
    curr_roi = frame[roi_top:, :]
    prev_roi = prev_frame[roi_top:, :]

    diff  = cv2.absdiff(curr_roi, prev_roi)
    gray  = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, BLUR_KERNEL, 0)
    _, thresh = cv2.threshold(blur, threshold, 255, cv2.THRESH_BINARY)

    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    result = []
    for cnt in contours:
        if cv2.contourArea(cnt) >= min_area:
            shifted = cnt.copy()
            shifted[:, :, 1] += roi_top    # offset y to full-frame space
            result.append(shifted)
    return result


def count_active_bodies(contours: list, max_mice: int = NUM_MICE) -> dict:
    """
    Count active bodies from detected contours; caps at max_mice.
    Clustered mice produce fewer large contours — this is the expected
    limitation of group-level tracking without identity assignment.
    """
    active = min(len(contours), max_mice)
    return {"active": active, "resting": max(0, max_mice - active)}


def classify_group_state(active_count: int, max_mice: int = NUM_MICE) -> str:
    """Map active body count to group state label."""
    if active_count >= ALL_ACTIVE_MIN:
        return "All Active"
    if active_count >= MOSTLY_ACTIVE_MIN:
        return "Mostly Active"
    if active_count == 1:
        return "Mostly Resting"
    return "All Resting"


def assign_dominant_zone(contours: list, frame_width: int) -> str:
    """
    Dominant zone (Left / Center / Right) from the combined centroid
    of all active contours. Falls back to Center when no contours.
    """
    if not contours:
        return "Center"
    all_pts = np.vstack([c.reshape(-1, 2) for c in contours])
    cx = float(all_pts[:, 0].mean())
    third = frame_width / 3
    if cx < third:
        return "Left"
    if cx < 2 * third:
        return "Center"
    return "Right"


def compute_motion_energy(contours: list, roi_area: int) -> float:
    """Total contour area / ROI area, clamped to [0.0, 1.0]."""
    if not contours or roi_area <= 0:
        return 0.0
    total = sum(cv2.contourArea(c) for c in contours)
    return float(min(total / roi_area, 1.0))


def compute_centroid_distances(centroids: list) -> dict:
    """
    Given a list of (x, y) tuples (one per detected body), return pairwise
    Euclidean distance statistics and a single-number spatial spread.

    Returns dict with keys: mean, min, max, spread.
    All values are NaN when fewer than 2 centroids are provided — pairwise
    distances are undefined with only one point.

    spatial_spread_px = sqrt(var(x) + var(y)):  RMS of per-axis standard
    deviations; a single number summarising how spread out the group is.
    """
    _nan = float("nan")
    if len(centroids) < 2:
        return {"mean": _nan, "min": _nan, "max": _nan, "spread": _nan}

    pts = np.array(centroids, dtype=np.float64)

    # All unique pairwise Euclidean distances
    dists = []
    n = len(pts)
    for i in range(n):
        for j in range(i + 1, n):
            dists.append(float(np.hypot(pts[j, 0] - pts[i, 0],
                                        pts[j, 1] - pts[i, 1])))

    spread = float(np.sqrt(np.var(pts[:, 0]) + np.var(pts[:, 1])))
    return {
        "mean":   round(float(np.mean(dists)), 2),
        "min":    round(float(np.min(dists)),  2),
        "max":    round(float(np.max(dists)),  2),
        "spread": round(spread, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_multi_tracking(
    video_path:     str = VIDEO_PATH,
    frame_skip:     int = FRAME_SKIP,
    max_frames:     int = None,
    progress_every: int = 5000,
) -> pd.DataFrame:
    """
    Process video and return per-frame tracking DataFrame.

    Columns: frame, timestamp_s, timestamp_clock, active_bodies, resting_bodies,
             total_motion_area, motion_energy, group_state, dominant_zone,
             centroids_x, centroids_y,
             mean_interanimal_dist_px, min_interanimal_dist_px,
             max_interanimal_dist_px, spatial_spread_px

    max_frames: number of decoded frames to process (None = full video).
    With frame_skip=2, each decoded frame represents 2 video frames.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {video_path}")

    info      = inspect_video(video_path)
    frame_w   = info["width"]
    roi_top   = info["roi_top_px"]
    roi_area  = info["roi_area_px"]
    video_fps = info["fps"]
    total_vid = info["frame_count"]

    if max_frames is None:
        max_frames = total_vid // frame_skip

    # OCR recording start time from first frame; fall back to RECORDING_START constant
    ret0, frame0 = cap.read()
    ocr_ts = extract_frame_timestamp(frame0) if ret0 else ""
    rec_start_hms  = ocr_ts if ocr_ts else RECORDING_START
    rec_start_secs = _hms_to_seconds(rec_start_hms)
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)    # rewind
    logger.info(f"Recording start (OCR): {ocr_ts!r}  ->  using {rec_start_hms}")

    rows       = []
    prev_frame = None
    decoded    = 0
    vid_frame  = 0      # absolute frame number in source video
    t0         = time.perf_counter()

    while decoded < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
        vid_frame += 1

        for _ in range(frame_skip - 1):
            if not cap.grab():
                break
            vid_frame += 1

        decoded += 1

        if prev_frame is None:
            prev_frame = frame
            continue

        contours    = detect_all_contours(frame, prev_frame, roi_top)
        counts      = count_active_bodies(contours)
        group_state = classify_group_state(counts["active"])
        zone        = assign_dominant_zone(contours, frame_w)
        energy      = compute_motion_energy(contours, roi_area)
        total_area  = sum(cv2.contourArea(c) for c in contours)

        # Per-contour centroids (full-frame coordinates, capped at NUM_MICE)
        centroids = []
        for cnt in contours[:NUM_MICE]:
            M = cv2.moments(cnt)
            if M["m00"] > 0:
                centroids.append((M["m10"] / M["m00"], M["m01"] / M["m00"]))

        dist = compute_centroid_distances(centroids)

        ts_s = vid_frame / video_fps
        rows.append({
            "frame":             vid_frame,
            "timestamp_s":       round(ts_s, 3),
            "timestamp_clock":   _seconds_to_hms(rec_start_secs + ts_s),
            "active_bodies":     counts["active"],
            "resting_bodies":    counts["resting"],
            "total_motion_area": round(total_area, 1),
            "motion_energy":     round(energy, 6),
            "group_state":       group_state,
            "dominant_zone":     zone,
            # Centroid columns (pipe-separated; empty string when no bodies)
            "centroids_x":              "|".join(str(round(cx)) for cx, _ in centroids),
            "centroids_y":              "|".join(str(round(cy)) for _, cy in centroids),
            # Distance columns (NaN when active_bodies < 2)
            "mean_interanimal_dist_px": dist["mean"],
            "min_interanimal_dist_px":  dist["min"],
            "max_interanimal_dist_px":  dist["max"],
            "spatial_spread_px":        dist["spread"],
        })

        prev_frame = frame

        if decoded % progress_every == 0:
            elapsed = time.perf_counter() - t0
            rate    = decoded / elapsed if elapsed > 0 else 1
            remain  = (max_frames - decoded) / rate
            em, es  = divmod(int(elapsed), 60)
            rm, rs  = divmod(int(remain),  60)
            logger.info(f"Decoded {decoded:>7,} / {max_frames:>7,} "
                        f"({decoded / max_frames * 100:.1f}%)  "
                        f"elapsed {em}m{es:02d}s  ETA {rm}m{rs:02d}s")

    cap.release()
    elapsed = time.perf_counter() - t0
    em, es  = divmod(int(elapsed), 60)
    df = pd.DataFrame(rows)
    logger.info(f"Tracking done: {len(df):,} rows in {em}m{es:02d}s")
    return df


def compute_group_rhythm(
    tracking_df: pd.DataFrame,
    fps:         float = FPS,
    bin_minutes: int   = RHYTHM_BIN_MINUTES,
) -> pd.DataFrame:
    """
    Bin tracking data into fixed time windows.
    Returns one row per bin with aggregate behavioral statistics.
    """
    bin_seconds = bin_minutes * 60.0
    df = tracking_df.copy()
    df["bin"] = (df["timestamp_s"] // bin_seconds).astype(int)

    def _dominant(s):
        vc = s.value_counts()
        return vc.index[0] if len(vc) > 0 else "All Resting"

    rhythm = df.groupby("bin").agg(
        bin_start_frame      = ("frame",         "min"),
        bin_end_frame        = ("frame",         "max"),
        mean_active_bodies   = ("active_bodies", "mean"),
        peak_active_bodies   = ("active_bodies", "max"),
        mean_motion_energy   = ("motion_energy", "mean"),
        dominant_group_state = ("group_state",   _dominant),
    ).reset_index(drop=True)

    active_frac = df.groupby("bin")["active_bodies"].apply(
        lambda x: (x >= 1).mean()
    ).values
    rhythm["active_minutes"] = (active_frac * bin_minutes).round(2)

    rhythm["timestamp"] = rhythm["bin_start_frame"].apply(
        lambda f: _seconds_to_hms(f / fps)
    )

    cols = [
        "timestamp", "bin_start_frame", "bin_end_frame",
        "mean_active_bodies", "peak_active_bodies",
        "mean_motion_energy", "dominant_group_state", "active_minutes",
    ]
    return rhythm[cols].round({"mean_active_bodies": 3, "mean_motion_energy": 5})


def compute_social_metrics(
    tracking_df: pd.DataFrame,
    fps:         float = FPS,
    bin_minutes: int   = RHYTHM_BIN_MINUTES,
) -> pd.DataFrame:
    """
    Bin inter-animal distance metrics into fixed time windows.

    Rows with active_bodies < 2 have NaN distances and are excluded from
    per-bin means (pandas agg skips NaN by default).

    Returns one row per bin with columns:
        timestamp | mean_interanimal_dist | min_interanimal_dist |
        huddling_flag | exploration_flag

    huddling_flag   = bin mean_interanimal_dist < HUDDLE_THRESHOLD_PX (80 px)
    exploration_flag = bin mean_interanimal_dist > EXPLORE_THRESHOLD_PX (200 px)
    Both flags are False when fewer than 2 bodies detected in the bin.
    """
    bin_seconds = bin_minutes * 60.0
    df = tracking_df.copy()
    df["bin"] = (df["timestamp_s"] // bin_seconds).astype(int)

    social = df.groupby("bin").agg(
        bin_start_frame       = ("frame",                    "min"),
        mean_interanimal_dist = ("mean_interanimal_dist_px", "mean"),
        min_interanimal_dist  = ("min_interanimal_dist_px",  "mean"),
    ).reset_index(drop=True)

    social["huddling_flag"]    = social["mean_interanimal_dist"] < HUDDLE_THRESHOLD_PX
    social["exploration_flag"] = social["mean_interanimal_dist"] > EXPLORE_THRESHOLD_PX

    # Bins where mean is NaN (no frames with ≥ 2 bodies) get False for both flags
    nan_mask = social["mean_interanimal_dist"].isna()
    social.loc[nan_mask, "huddling_flag"]    = False
    social.loc[nan_mask, "exploration_flag"] = False

    social["timestamp"] = social["bin_start_frame"].apply(
        lambda f: _seconds_to_hms(f / fps)
    )

    return social[[
        "timestamp", "mean_interanimal_dist", "min_interanimal_dist",
        "huddling_flag", "exploration_flag",
    ]].round({"mean_interanimal_dist": 2, "min_interanimal_dist": 2})


def export_group_summary(
    tracking_df: pd.DataFrame,
    fps:         float = FPS,
    output_path: str   = SUMMARY_CSV_PATH,
) -> None:
    """Compute group rhythm and save to output_path."""
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    rhythm_df = compute_group_rhythm(tracking_df, fps=fps)
    rhythm_df.to_csv(output_path, index=False)
    logger.info(f"Group summary saved: {output_path}  ({len(rhythm_df)} bins)")


def export_annotated_video(
    video_path:       str,
    tracking_df:      pd.DataFrame,
    output_path:      str,
    duration_seconds: int = 60,
    start_frame:      int = 0,
) -> None:
    """
    Export annotated video for a clip starting at start_frame.

    Text overlays are driven by tracking_df (CSV predictions) with last-known-value
    fallback for frames not in the CSV (every other frame due to FRAME_SKIP=2).
    Bounding boxes are re-detected frame-by-frame in real-time since contour
    positions are not stored in the CSV.

    Overlays:
      Top-right   — Active: X / 5 (state-colour-coded)
      Per contour — bounding box + body number 1..N (real-time)
      Bottom-left — Motion energy, Group state
      Bottom-right — Clock: HH:MM:SS and Frame XXXXXX / XXXXXX
      Horizontal grey line at ROI boundary
    """
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    raw_path = output_path.replace(".mp4", "_raw.mp4")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {video_path}")

    info     = inspect_video(video_path)
    w, h     = info["width"], info["height"]
    roi_top  = info["roi_top_px"]
    roi_area = info["roi_area_px"]
    fps      = info["fps"]
    total_vid_frames = info["frame_count"]

    if start_frame > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    max_frames = int(duration_seconds * fps)
    # cap to what's actually available from start_frame
    max_frames = min(max_frames, total_vid_frames - start_frame)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(raw_path, fourcc, fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter failed: {raw_path}")

    # Build lookup: abs_video_frame -> CSV row dict
    # tracking_df has abs frame numbers; filter to clip window for speed
    clip_end = start_frame + max_frames
    clip_df  = tracking_df[
        (tracking_df["frame"] >= start_frame) &
        (tracking_df["frame"] <  clip_end)
    ]
    frame_lookup = clip_df.set_index("frame").to_dict("index")

    FONT        = cv2.FONT_HERSHEY_SIMPLEX
    FS          = 0.55
    THICK       = 1
    LINE_H      = 22
    PAD         = 5
    DEFAULT_CLR = (180, 180, 180)
    STATE_COLORS = {
        "All Active":     (0, 255,   0),   # green
        "Mostly Active":  (0, 255, 255),   # yellow
        "Mostly Resting": (0, 165, 255),   # orange
        "All Resting":    (0,   0, 255),   # red
    }

    def _bg_rect(fr, x1, y1, x2, y2, alpha=0.55):
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w - 1, x2), min(h - 1, y2)
        if x2 <= x1 or y2 <= y1:
            return
        sub = fr[y1:y2, x1:x2]
        fr[y1:y2, x1:x2] = cv2.addWeighted(np.zeros_like(sub), alpha, sub, 1 - alpha, 0)

    def _text_block(fr, lines, x, y):
        if not lines:
            return
        max_w = max(cv2.getTextSize(t, FONT, FS, THICK)[0][0] for t, _ in lines)
        _bg_rect(fr, x - PAD, y - PAD, x + max_w + PAD, y + len(lines) * LINE_H + PAD)
        for i, (text, clr) in enumerate(lines):
            cv2.putText(fr, text, (x, y + i * LINE_H + LINE_H - 5),
                        FONT, FS, clr, THICK, cv2.LINE_AA)

    def _text_block_right(fr, lines, x_right, y):
        if not lines:
            return
        max_w = max(cv2.getTextSize(t, FONT, FS, THICK)[0][0] for t, _ in lines)
        x0 = x_right - max_w
        _bg_rect(fr, x0 - PAD, y - PAD, x_right + PAD, y + len(lines) * LINE_H + PAD)
        for i, (text, clr) in enumerate(lines):
            tw = cv2.getTextSize(text, FONT, FS, THICK)[0][0]
            cv2.putText(fr, text, (x_right - tw, y + i * LINE_H + LINE_H - 5),
                        FONT, FS, clr, THICK, cv2.LINE_AA)

    prev_frame     = None
    current_state  = "All Resting"
    current_energy = 0.0
    current_active = 0
    current_clock  = ""
    frame_idx      = 0
    t0             = time.perf_counter()

    while frame_idx < max_frames:
        ret, frame = cap.read()
        if not ret:
            break

        abs_frame = start_frame + frame_idx

        # Update state from CSV lookup; keep last-known values on cache miss
        if abs_frame in frame_lookup:
            row            = frame_lookup[abs_frame]
            current_state  = row["group_state"]
            current_energy = row["motion_energy"]
            current_active = row["active_bodies"]
            current_clock  = row.get("timestamp_clock", "")

        state_clr = STATE_COLORS.get(current_state, DEFAULT_CLR)

        # Re-detect contours in real-time for bounding box positions
        contours = []
        if prev_frame is not None:
            contours = detect_all_contours(frame, prev_frame, roi_top)

        # Per-contour bounding boxes + body number
        for i, cnt in enumerate(contours[:NUM_MICE]):
            x_, y_, cw_, ch_ = cv2.boundingRect(cnt)
            cv2.rectangle(frame, (x_, y_), (x_ + cw_, y_ + ch_), state_clr, 2)
            cv2.putText(frame, str(i + 1), (x_ + 2, y_ + 14),
                        FONT, 0.45, state_clr, 1, cv2.LINE_AA)

        # ROI boundary line
        cv2.line(frame, (0, roi_top), (w, roi_top), (80, 80, 80), 1)

        # Top-right: active count
        _text_block_right(frame, [
            (f"Active: {current_active} / {NUM_MICE}", state_clr),
        ], x_right=w - PAD, y=roi_top + 5)

        # Bottom-left: motion energy + group state
        _text_block(frame, [
            (f"Motion energy: {current_energy:.4f}", DEFAULT_CLR),
            (f"Group: {current_state}",              state_clr),
        ], x=10, y=h - 2 * LINE_H - PAD * 2)

        # Bottom-right: clock + frame counter (two lines)
        clock_str = f"Clock: {current_clock}" if current_clock else f"Frame {abs_frame:06d}"
        _text_block_right(frame, [
            (clock_str,                                          DEFAULT_CLR),
            (f"Frame {abs_frame:06d} / {clip_end:06d}", DEFAULT_CLR),
        ], x_right=w - PAD, y=h - 2 * LINE_H - PAD * 2)

        writer.write(frame)
        prev_frame = frame
        frame_idx += 1

    cap.release()
    writer.release()

    elapsed = time.perf_counter() - t0
    logger.info(f"Wrote {frame_idx} frames in {elapsed:.1f}s ({frame_idx / elapsed:.0f} FPS)")

    subprocess.run([
        "ffmpeg", "-y", "-i", raw_path,
        "-vcodec", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
        output_path,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    out_mb = os.path.getsize(output_path) / (1024 ** 2)
    logger.info(f"Re-encoded: {output_path}  ({out_mb:.1f} MB)")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hms_to_seconds(hms: str) -> int:
    """Parse 'HH:MM:SS' to integer seconds from midnight."""
    h, m, s = hms.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _seconds_to_hms(seconds: float) -> str:
    """Format seconds-from-midnight as 'HH:MM:SS'; wraps at midnight."""
    s = int(seconds) % 86400
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
