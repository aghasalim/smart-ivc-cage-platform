"""
run_yolo_tracking.py
Process the full multiple_mouse.mp4 with YOLO + Hungarian tracker.
Exports per-mouse position data to outputs/multi_mouse_tracked.csv.

Headless — no display window. Runs overnight if needed.

Usage:
    cd multi_animal_tracking
    python run_yolo_tracking.py
"""

import csv
import math
import os
import sys
import time
from collections import deque

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment
from ultralytics import YOLO

# ── Constants ─────────────────────────────────────────────────────────────────
VIDEO_PATH       = r"C:\Users\grazv\Desktop\multiple_mouse.mp4"
YOLO_MODEL_PATH  = "outputs/Fine_Tuned.pt"
OUTPUT_CSV       = "outputs/multi_mouse_tracked.csv"
FRAME_SKIP       = 2
YOLO_CONF        = 0.35
YOLO_DEADZONE_PX = 5
MAX_MICE         = 5
MAX_LOST_FRAMES  = 30
DISTANCE_THRESHOLD = 150
FPS              = 25.0
TAIL_LENGTH      = 50
PROGRESS_EVERY   = 10_000   # video frames

CSV_HEADER = [
    "frame", "timestamp_s", "mouse_id",
    "cx", "cy", "conf",
    "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2",
    "distance_px",
]

# ── MouseTracker (identical logic to live_demo.py) ────────────────────────────
class MouseTracker:
    def __init__(self):
        self.tracks    = {}
        self.next_id   = 0
        self._labels   = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        self._colors   = [(0,255,0),(255,200,0),(0,165,255),(255,0,255),(0,255,255)]
        # Stats
        self.total_created = 0
        self._birth_frame  = {}   # track_id -> frame it was first created
        self._death_frame  = {}   # track_id -> frame it was pruned
        self._last_frame   = {}   # track_id -> most recent frame seen

    def _make_track(self, det, frame_idx):
        tid   = self.next_id
        self.next_id += 1
        self.total_created += 1
        label = self._labels[tid % len(self._labels)]
        self.tracks[tid] = {
            "cx": det["cx"], "cy": det["cy"],
            "lost": 0, "label": label,
            "history": deque(maxlen=TAIL_LENGTH),
            "last_dist": 0.0,
        }
        self.tracks[tid]["history"].append((det["cx"], det["cy"]))
        self._birth_frame[tid] = frame_idx
        self._last_frame[tid]  = frame_idx
        return tid

    def update(self, detections, frame_idx):
        """
        Args:
            detections: list of dicts  cx, cy, x1, y1, x2, y2, conf
            frame_idx:  current video frame number
        Returns:
            list of dicts enriched with track_id, label, distance_px
        """
        # Age all tracks; prune dead ones
        to_prune = [tid for tid, t in self.tracks.items()
                    if t["lost"] > MAX_LOST_FRAMES]
        for tid in to_prune:
            self._death_frame[tid] = frame_idx
            del self.tracks[tid]

        if not detections:
            for t in self.tracks.values():
                t["lost"] += 1
            return []

        if not self.tracks:
            result = []
            for det in detections[:MAX_MICE]:
                tid = self._make_track(det, frame_idx)
                det["track_id"] = tid
                det["label"]    = self.tracks[tid]["label"]
                det["distance_px"] = 0.0
                self.tracks[tid]["last_dist"] = 0.0
                result.append(det)
            return result

        track_ids = list(self.tracks.keys())
        n_t, n_d  = len(track_ids), len(detections)
        cost      = np.zeros((n_t, n_d), dtype=np.float32)
        for i, tid in enumerate(track_ids):
            tx, ty = self.tracks[tid]["cx"], self.tracks[tid]["cy"]
            for j, det in enumerate(detections):
                cost[i, j] = math.hypot(tx - det["cx"], ty - det["cy"])

        row_ind, col_ind = linear_sum_assignment(cost)

        matched_t = set()
        matched_d = set()
        result    = []

        for r, c in zip(row_ind, col_ind):
            if cost[r, c] >= DISTANCE_THRESHOLD:
                continue
            tid = track_ids[r]
            det = detections[c]

            raw_dist = cost[r, c]
            dist     = 0.0 if raw_dist < YOLO_DEADZONE_PX else raw_dist

            self.tracks[tid]["cx"]        = det["cx"]
            self.tracks[tid]["cy"]        = det["cy"]
            self.tracks[tid]["lost"]      = 0
            self.tracks[tid]["last_dist"] = dist
            self.tracks[tid]["history"].append((det["cx"], det["cy"]))
            self._last_frame[tid]         = frame_idx

            det["track_id"]    = tid
            det["label"]       = self.tracks[tid]["label"]
            det["distance_px"] = dist
            matched_t.add(r)
            matched_d.add(c)
            result.append(det)

        # Unmatched detections → new tracks
        for j, det in enumerate(detections):
            if j not in matched_d and len(self.tracks) < MAX_MICE:
                tid = self._make_track(det, frame_idx)
                det["track_id"]    = tid
                det["label"]       = self.tracks[tid]["label"]
                det["distance_px"] = 0.0
                result.append(det)

        # Increment lost counter for unmatched tracks
        for i, tid in enumerate(track_ids):
            if i not in matched_t:
                self.tracks[tid]["lost"] += 1

        return result

    def persistence_stats(self, final_frame):
        """Return dict of track duration stats (in decoded frames)."""
        durations = []
        for tid in range(self.next_id):
            birth = self._birth_frame.get(tid, 0)
            death = self._death_frame.get(tid, self._last_frame.get(tid, birth))
            durations.append(death - birth)
        if not durations:
            return {}
        return {
            "total_tracks":  len(durations),
            "mean_duration": round(sum(durations) / len(durations), 1),
            "max_duration":  max(durations),
            "min_duration":  min(durations),
            "median_duration": sorted(durations)[len(durations)//2],
        }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    os.makedirs("outputs", exist_ok=True)

    # Validate inputs
    if not os.path.exists(VIDEO_PATH):
        print(f"ERROR: Video not found: {VIDEO_PATH}")
        sys.exit(1)
    if not os.path.exists(YOLO_MODEL_PATH):
        print(f"ERROR: YOLO model not found: {YOLO_MODEL_PATH}")
        sys.exit(1)

    # Load YOLO
    print(f"Loading YOLO model: {YOLO_MODEL_PATH}")
    yolo = YOLO(YOLO_MODEL_PATH)
    print("YOLO loaded.")

    # Open video
    cap        = cv2.VideoCapture(VIDEO_PATH)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    vid_fps    = cap.get(cv2.CAP_PROP_FPS) or FPS
    print(f"Video: {VIDEO_PATH}")
    print(f"  Frames : {total_frames:,}")
    print(f"  FPS    : {vid_fps:.1f}")
    print(f"  Duration: {total_frames/vid_fps/3600:.2f} hours")
    print(f"  Decoded frames (FRAME_SKIP={FRAME_SKIP}): "
          f"{total_frames // FRAME_SKIP:,}")

    # Estimate processing time (rough: 120ms/frame on CPU)
    est_s = (total_frames // FRAME_SKIP) * 0.12
    print(f"  Est. processing time: {est_s/3600:.1f} h  (CPU; GPU will be faster)")
    print()

    tracker     = MouseTracker()
    t_start     = time.time()
    frame_idx   = 0      # video frame counter
    decoded     = 0      # frames actually run through YOLO
    rows_written = 0

    csv_fh = open(OUTPUT_CSV, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_fh)
    writer.writerow(CSV_HEADER)

    print(f"Writing to: {OUTPUT_CSV}")
    print(f"Progress every {PROGRESS_EVERY:,} video frames")
    print("-" * 72)

    try:
        while True:
            # Skip frames
            for _ in range(FRAME_SKIP - 1):
                if not cap.grab():
                    break

            ret, frame = cap.read()
            if not ret:
                break

            frame_idx += FRAME_SKIP
            decoded   += 1
            ts_s       = frame_idx / vid_fps

            # YOLO inference
            results    = yolo(frame, conf=YOLO_CONF, verbose=False)
            raw_boxes  = results[0].boxes
            detections = []
            for box in raw_boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                detections.append({
                    "cx": (x1+x2)//2, "cy": (y1+y2)//2,
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "conf": conf,
                })

            # Hungarian tracking
            tracked = tracker.update(detections, frame_idx)

            # Write CSV rows
            for det in tracked:
                writer.writerow([
                    frame_idx,
                    round(ts_s, 3),
                    det["label"],
                    det["cx"], det["cy"],
                    round(det["conf"], 4),
                    det["x1"], det["y1"], det["x2"], det["y2"],
                    round(det["distance_px"], 2),
                ])
                rows_written += 1

            # Progress report
            if frame_idx % PROGRESS_EVERY == 0:
                elapsed   = time.time() - t_start
                pct       = frame_idx / total_frames * 100
                remaining = total_frames - frame_idx
                fps_proc  = decoded / elapsed if elapsed > 0 else 0
                eta_s     = remaining / (FRAME_SKIP * fps_proc) if fps_proc > 0 else 0
                active    = sum(1 for t in tracker.tracks.values() if t["lost"] == 0)
                print(
                    f"Frame {frame_idx:>7,} / {total_frames:,}  "
                    f"({pct:5.1f}%)  "
                    f"{active} mice tracked  |  "
                    f"elapsed {elapsed/60:.0f}m {elapsed%60:.0f}s  "
                    f"ETA {eta_s/60:.0f}m"
                )
                csv_fh.flush()

    except KeyboardInterrupt:
        print("\nInterrupted — flushing CSV...")
    finally:
        csv_fh.close()
        cap.release()

    elapsed = time.time() - t_start
    print("-" * 72)
    print(f"Processing complete.")
    print(f"  Video frames processed : {frame_idx:,}")
    print(f"  Decoded frames         : {decoded:,}")
    print(f"  Total CSV rows written : {rows_written:,}")
    print(f"  Total tracks created   : {tracker.total_created}")
    print(f"  Wall time              : {elapsed/3600:.2f} h  "
          f"({elapsed/decoded*1000:.0f} ms/frame)")

    stats = tracker.persistence_stats(frame_idx)
    if stats:
        print(f"\nTrack persistence stats (in video frames, FRAME_SKIP={FRAME_SKIP}):")
        for k, v in stats.items():
            print(f"  {k:<22}: {v}")

    # Print first 10 rows
    print(f"\nFirst 10 rows of {OUTPUT_CSV}:")
    with open(OUTPUT_CSV, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= 11:
                break
            print(" ", line.rstrip())

    print(f"\nDone — {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
