from ultralytics import YOLO
from scipy.optimize import linear_sum_assignment
from collections import deque, defaultdict
import numpy as np
import joblib
import json
import cv2
import os
import sys

# ── CONSTANTS ───────────────────────────────────────────────────────────────
YOLO_MODEL_PATH       = "outputs/Fine_Tuned.pt"
RF_MODEL_PATH         = "outputs/state_classifier_v3.pkl"
FEATURE_COLS_PATH     = "outputs/feature_columns_v3.json"
CAMERA_INDEX          = 1
YOLO_CONF             = 0.35
YOLO_DEADZONE_PX      = 5       # ignore movement below this — YOLO box jitter
WAKE_THRESHOLD        = 2.5     # movement above this = active
QUIET_STREAK_SLEEP    = 450     # frames of stillness for Possible Sleep (30s at 15fps)
SMOOTH_WINDOW         = 15
MAX_MICE              = 5
MAX_LOST_FRAMES       = 30
DISTANCE_THRESHOLD    = 150
TAIL_LENGTH           = 50

# ── STATE COLORS ────────────────────────────────────────────────────────────
STATE_COLORS = {
    "Wake / Active":  (0, 255, 0),     # green
    "Quiet / Rest":   (0, 255, 255),   # yellow
    "Possible Sleep": (255, 100, 0),   # blue
}

# ── MOUSE TRACKER ───────────────────────────────────────────────────────────
class MouseTracker:
    def __init__(self):
        self.tracks = {}
        self.next_id = 0
        self.labels = "ABCDEFGHIJ"
        self.colors = [
            (0, 255, 0),    # green
            (255, 200, 0),  # cyan
            (0, 165, 255),  # orange
            (255, 0, 255),  # magenta
            (0, 255, 255),  # yellow
        ]

    def update(self, detections):
        if not detections:
            for tid in self.tracks:
                self.tracks[tid]['lost'] += 1
            self.tracks = {k: v for k, v in self.tracks.items()
                          if v['lost'] <= MAX_LOST_FRAMES}
            return []

        if not self.tracks:
            for det in detections:
                tid = self.next_id
                self.next_id += 1
                label = self.labels[tid % len(self.labels)]
                color = self.colors[tid % len(self.colors)]
                self.tracks[tid] = {
                    'cx': det['cx'], 'cy': det['cy'],
                    'lost': 0, 'label': label, 'color': color,
                    'history': deque(maxlen=TAIL_LENGTH),
                    'distances': deque(maxlen=SMOOTH_WINDOW),
                    'quiet_streak': 0,
                    'state': 'Wake / Active'
                }
                self.tracks[tid]['history'].append((det['cx'], det['cy']))
                det['track'] = self.tracks[tid]
            return detections

        track_ids = list(self.tracks.keys())
        cost = np.zeros((len(track_ids), len(detections)))
        for i, tid in enumerate(track_ids):
            for j, det in enumerate(detections):
                dx = self.tracks[tid]['cx'] - det['cx']
                dy = self.tracks[tid]['cy'] - det['cy']
                cost[i, j] = np.sqrt(dx**2 + dy**2)

        row_ind, col_ind = linear_sum_assignment(cost)

        matched_tracks = set()
        matched_dets = set()

        for i, j in zip(row_ind, col_ind):
            if cost[i, j] < DISTANCE_THRESHOLD:
                tid = track_ids[i]
                det = detections[j]

                # Compute distance with deadzone
                dx = det['cx'] - self.tracks[tid]['cx']
                dy = det['cy'] - self.tracks[tid]['cy']
                dist = np.sqrt(dx**2 + dy**2)
                if dist < YOLO_DEADZONE_PX:
                    dist = 0.0

                self.tracks[tid]['distances'].append(dist)
                if dist == 0.0:
                    self.tracks[tid]['quiet_streak'] += 1
                else:
                    self.tracks[tid]['quiet_streak'] = 0

                self.tracks[tid]['cx'] = det['cx']
                self.tracks[tid]['cy'] = det['cy']
                self.tracks[tid]['lost'] = 0
                self.tracks[tid]['history'].append((det['cx'], det['cy']))

                # Classify state per mouse
                avg_movement = np.mean(self.tracks[tid]['distances']) if self.tracks[tid]['distances'] else 0
                qs = self.tracks[tid]['quiet_streak']

                if avg_movement > WAKE_THRESHOLD:
                    self.tracks[tid]['state'] = 'Wake / Active'
                elif qs >= QUIET_STREAK_SLEEP:
                    self.tracks[tid]['state'] = 'Possible Sleep'
                else:
                    self.tracks[tid]['state'] = 'Quiet / Rest'

                det['track'] = self.tracks[tid]
                matched_tracks.add(tid)
                matched_dets.add(j)

        for j, det in enumerate(detections):
            if j not in matched_dets and len(self.tracks) < MAX_MICE:
                tid = self.next_id
                self.next_id += 1
                label = self.labels[tid % len(self.labels)]
                color = self.colors[tid % len(self.colors)]
                self.tracks[tid] = {
                    'cx': det['cx'], 'cy': det['cy'],
                    'lost': 0, 'label': label, 'color': color,
                    'history': deque(maxlen=TAIL_LENGTH),
                    'distances': deque(maxlen=SMOOTH_WINDOW),
                    'quiet_streak': 0,
                    'state': 'Wake / Active'
                }
                self.tracks[tid]['history'].append((det['cx'], det['cy']))
                det['track'] = self.tracks[tid]

        for tid in track_ids:
            if tid not in matched_tracks:
                self.tracks[tid]['lost'] += 1

        self.tracks = {k: v for k, v in self.tracks.items()
                      if v['lost'] <= MAX_LOST_FRAMES}

        return detections

# ── MAIN ────────────────────────────────────────────────────────────────────
def main():
    # Load YOLO
    if not os.path.exists(YOLO_MODEL_PATH):
        print(f"ERROR: YOLO model not found at {YOLO_MODEL_PATH}")
        sys.exit(1)
    yolo = YOLO(YOLO_MODEL_PATH)
    print(f"YOLO loaded: {YOLO_MODEL_PATH}")

    # Open camera
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"ERROR: Cannot open camera {CAMERA_INDEX}")
        sys.exit(1)
    print(f"Camera {CAMERA_INDEX} opened")

    tracker = MouseTracker()
    frame_count = 0

    print("Running — press Q to quit")
    print("-" * 60)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1

        # YOLO detection
        results = yolo(frame, conf=YOLO_CONF, verbose=False)
        boxes = results[0].boxes

        # Extract detections
        detections = []
        for box in boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            detections.append({
                'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                'cx': cx, 'cy': cy, 'conf': conf
            })

        # Update tracker
        detections = tracker.update(detections)

        # Draw on frame
        display = frame.copy()

        # Count states
        states = [t['state'] for t in tracker.tracks.values() if t['lost'] == 0]
        dominant_state = max(set(states), key=states.count) if states else "No detection"
        state_color = STATE_COLORS.get(dominant_state, (255, 255, 255))

        # Draw per-mouse boxes and tails
        for det in detections:
            if 'track' not in det:
                continue
            t = det['track']
            color = STATE_COLORS.get(t['state'], (255, 255, 255))

            # Bounding box
            thickness = 3 if t['label'] == 'A' else 2
            cv2.rectangle(display, (det['x1'], det['y1']), (det['x2'], det['y2']),
                         color, thickness)

            # Label: ID + confidence + state
            label_text = f"{t['label']} {det['conf']:.2f} {t['state']}"
            cv2.rectangle(display,
                         (det['x1'], det['y1'] - 20),
                         (det['x1'] + len(label_text) * 9, det['y1']),
                         (0, 0, 0), -1)
            cv2.putText(display, label_text,
                       (det['x1'] + 2, det['y1'] - 5),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

            # Centroid dot
            cv2.circle(display, (det['cx'], det['cy']), 4, color, -1)

            # Trajectory tail
            pts = list(t['history'])
            for i in range(1, len(pts)):
                alpha = i / len(pts)
                c = tuple(int(v * alpha) for v in color)
                cv2.line(display, pts[i-1], pts[i], c, 1)

        # Info overlay
        active = len(tracker.tracks)
        tracked_labels = sorted([t['label'] for t in tracker.tracks.values() if t['lost'] == 0])

        overlay_lines = [
            (f"Mice   : {len(states)}  [{','.join(tracked_labels)}]", state_color),
            (f"State  : {dominant_state}", state_color),
        ]

        y_pos = 25
        for text, color in overlay_lines:
            cv2.rectangle(display, (5, y_pos - 18), (5 + len(text) * 10, y_pos + 4), (0, 0, 0), -1)
            cv2.putText(display, text, (8, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            y_pos += 25

        # Print status every 2 seconds (~60 frames at 30fps webcam)
        if frame_count % 60 == 0:
            state_summary = {}
            for t in tracker.tracks.values():
                if t['lost'] == 0:
                    s = t['state']
                    state_summary[s] = state_summary.get(s, 0) + 1
            print(f"[Frame {frame_count}]  Mice:{len(states)}  Tracked:{tracked_labels}  States:{state_summary}")

        cv2.imshow("IVC Cage — Live Detection + Classification", display)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
    print("Done")

if __name__ == "__main__":
    main()
