# Swap cv2.VideoCapture("mouse_data.avi") with cv2.VideoCapture(0) for live camera feed.
"""
realtime_monitor_yolo.py
Real-time behavioral monitoring using YOLOv8 for mouse detection.

Drop-in replacement for realtime_monitor.py — identical behavioral classification
pipeline (RF v3 / SGD / ensemble / smoothing / CSV) with YOLO replacing the
frame-difference + contour detection step.

Falls back automatically to motion detection if YOLO_MODEL_PATH is not found.

Usage:
    python realtime_monitor_yolo.py
    python realtime_monitor_yolo.py --max-frames 450   # 30-second smoke test
"""

import os
import sys
import csv
import json
import math
import signal
import argparse
import logging
import logging.handlers
from collections import deque, Counter
from typing import Optional

import joblib
import cv2
import numpy as np
import pytesseract

if sys.platform == "win32":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_PATH = "outputs/realtime_monitor_yolo.log"
os.makedirs("outputs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=10_000_000, backupCount=3
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from behavior_analysis import (
    VIDEO_PATH,
    FPS, FRAME_SKIP, FRAME_WIDTH, FRAME_HEIGHT, ROI_TOP_FRACTION,
    MOTION_THRESHOLD, MIN_CONTOUR_AREA, BLUR_KERNEL,
    SMOOTH_WINDOW, ACTIVITY_SMOOTH_WINDOW,
    SMOOTH_INACTIVITY_THRESHOLD, WAKE_THRESHOLD, POSSIBLE_SLEEP_MIN_SECONDS,
    STEREO_WINDOW_FRAMES, STEREO_SPATIAL_RADIUS_PX,
    ZONE_NAMES_9, PERIMETER_ZONES,
    smooth_positions, extract_frame_timestamp,
)

try:
    from postprocess_states import (
        smooth_state_predictions,
        CONSECUTIVE_QUIET_FOR_SLEEP,
        CONSECUTIVE_ACTIVE_TO_WAKE,
    )
except ImportError:
    CONSECUTIVE_QUIET_FOR_SLEEP = 3
    def smooth_state_predictions(predictions):  # noqa: E302
        return list(predictions)

# ── YOLO config ───────────────────────────────────────────────────────────────
YOLO_MODEL_PATH        = "outputs/mouse_finetuned_v2.pt"
YOLO_CONF_THRESHOLD    = 0.35
YOLO_FALLBACK_TO_MOTION = True   # use motion detection if YOLO model not found

# ── Output ────────────────────────────────────────────────────────────────────
REALTIME_SUMMARY_PATH   = "outputs/realtime_yolo_summary.csv"
STATUS_INTERVAL_FRAMES  = 150
SUMMARY_INTERVAL_FRAMES = 600

# ── RF / SGD / ensemble model paths (unchanged from realtime_monitor.py) ─────
STATE_MODEL_PATH     = "outputs/state_classifier.pkl"
BEHAVIOR_MODEL_PATH  = "outputs/behavior_classifier.pkl"
FEATURE_COLUMNS_PATH = "outputs/feature_columns.json"
SGD_MODEL_PATH       = "outputs/sgd_online_classifier.pkl"
SGD_V2_MODEL_PATH    = "outputs/sgd_v2_classifier.pkl"
ENSEMBLE_CONFIG_PATH = "outputs/ensemble_config.json"
ONLINE_WINDOW_SIZE   = 450
V3_MODEL_PATH        = "outputs/state_classifier_v3.pkl"
V3_FEATURES_PATH     = "outputs/feature_columns_v3.json"
TEMPORAL_WINDOW      = 450
_ALL_STATE_CLASSES   = ["Wake / Active", "Quiet / Rest", "Possible Sleep"]

SUMMARY_HEADER = [
    "timestamp", "dominant_state", "total_distance_px",
    "active_frames", "inactive_frames", "dominant_zone",
    "thigmotaxis_ratio", "stereotypy_flag", "mice_detected",
]

# ── Zone helpers ──────────────────────────────────────────────────────────────
_ROI_Y = int(FRAME_HEIGHT * ROI_TOP_FRACTION)
_ROI_H = FRAME_HEIGHT - _ROI_Y
_X1    = FRAME_WIDTH / 3
_X2    = 2 * FRAME_WIDTH / 3
_Y1    = _ROI_Y + _ROI_H / 3
_Y2    = _ROI_Y + 2 * _ROI_H / 3

def _zone_for_point(x, y):
    col = 0 if x < _X1 else (1 if x < _X2 else 2)
    row = 0 if y < _Y1 else (1 if y < _Y2 else 2)
    return ZONE_NAMES_9[row][col]

# ── Display helpers ───────────────────────────────────────────────────────────
_STATE_COLORS = {
    "Wake / Active":  (0, 255, 0),
    "Quiet / Rest":   (0, 255, 255),
    "Possible Sleep": (255, 0, 0),
}

def _put_text_bg(img, text, pos, color):
    cv2.rectangle(img, (pos[0]-2, pos[1]-18),
                  (pos[0] + len(text)*10, pos[1]+4), (0, 0, 0), -1)
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

def _draw_yolo_boxes(img, yolo_boxes, primary_state):
    """Draw all YOLO bounding boxes in the state colour.
    Primary box (index 0, largest) uses linewidth=3; others use linewidth=2."""
    color = _STATE_COLORS.get(primary_state, (255, 255, 255))
    for i, (x1, y1, x2, y2, conf) in enumerate(yolo_boxes):
        thickness = 3 if i == 0 else 2
        cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
        cv2.circle(img, ((x1+x2)//2, (y1+y2)//2), 4, color, -1)
        label = f"M{i+1} {conf:.2f}"
        cv2.rectangle(img, (x1, y1-22), (x1+len(label)*10, y1), (0,0,0), -1)
        cv2.putText(img, label, (x1, y1-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

# ── RF model loader ───────────────────────────────────────────────────────────
def _load_rf_models():
    try:
        if not all(os.path.exists(p) for p in
                   [STATE_MODEL_PATH, BEHAVIOR_MODEL_PATH, FEATURE_COLUMNS_PATH]):
            return None
        state_clf = joblib.load(STATE_MODEL_PATH)
        beh_clf   = joblib.load(BEHAVIOR_MODEL_PATH)
        with open(FEATURE_COLUMNS_PATH) as f:
            meta = json.load(f)
        return state_clf, beh_clf, meta
    except Exception as e:
        logger.warning(f"Could not load RF models: {e} — using threshold fallback")
        return None

# ── Clock helpers ─────────────────────────────────────────────────────────────
def _parse_ocr_hms(ts):
    try:
        h, m, s = ts.split(" ")[1].split(":")
        return int(h), int(m), int(s)
    except (IndexError, ValueError, AttributeError):
        return None

def _clock_str(base_hms, frame_idx):
    if base_hms is None:
        total = frame_idx / FPS
    else:
        total = base_hms[0]*3600 + base_hms[1]*60 + base_hms[2] + frame_idx/FPS
    h = int(total // 3600) % 24
    m = int(total  % 3600) // 60
    s = int(total  % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# ── YOLO detection ────────────────────────────────────────────────────────────
def _yolo_detect(yolo_model, frame, conf_threshold):
    """Run YOLO on frame. Returns list of (x1,y1,x2,y2,conf) sorted largest→smallest."""
    results = yolo_model(frame, conf=conf_threshold, verbose=False)
    boxes_raw = results[0].boxes
    detections = []
    for box in boxes_raw:
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        conf = float(box.conf[0])
        detections.append((x1, y1, x2, y2, conf))
    # Sort by box area descending so index 0 is always the largest (primary) mouse
    detections.sort(key=lambda b: (b[2]-b[0])*(b[3]-b[1]), reverse=True)
    return detections

# ── Summary row ───────────────────────────────────────────────────────────────
def _flush_summary_row(writer, fh, clock, states, zones, behaviors,
                        total_dist, mice_counts):
    if not states:
        return
    dom_state  = Counter(states).most_common(1)[0][0]
    dom_zone   = Counter(zones).most_common(1)[0][0]
    active_n   = sum(1 for s in states if s == "Wake / Active")
    inactive_n = len(states) - active_n
    perimeter  = sum(1 for z in zones if z in PERIMETER_ZONES)
    thigmo     = round(perimeter / len(zones), 4)
    rep_rate   = sum(1 for b in behaviors if b == "Repetitive") / len(behaviors)
    avg_mice   = round(sum(mice_counts) / len(mice_counts), 2) if mice_counts else 0
    writer.writerow([
        clock, dom_state, round(total_dist, 2),
        active_n, inactive_n, dom_zone, thigmo, bool(rep_rate > 0.10), avg_mice,
    ])
    fh.flush()

# ── Main loop ─────────────────────────────────────────────────────────────────
def run(video_path=VIDEO_PATH, max_video_frames=None):
    os.makedirs("outputs", exist_ok=True)

    # ── Load YOLO model ───────────────────────────────────────────────────────
    yolo_model = None
    use_yolo   = False
    if os.path.exists(YOLO_MODEL_PATH):
        try:
            from ultralytics import YOLO
            yolo_model = YOLO(YOLO_MODEL_PATH)
            use_yolo   = True
            logger.info(f"YOLO      : {YOLO_MODEL_PATH}  (conf>={YOLO_CONF_THRESHOLD})")
        except Exception as _e:
            logger.warning(f"YOLO      : load failed ({_e}) — falling back to motion detection")
    else:
        if YOLO_FALLBACK_TO_MOTION:
            logger.warning(f"YOLO      : {YOLO_MODEL_PATH} not found — "
                           f"falling back to motion detection")
        else:
            raise FileNotFoundError(f"YOLO model not found: {YOLO_MODEL_PATH}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {video_path}")

    ret, first_frame = cap.read()
    if not ret:
        cap.release()
        raise RuntimeError("Cannot read first frame")

    ts0       = extract_frame_timestamp(first_frame)
    base_hms  = _parse_ocr_hms(ts0)
    prev_gray = cv2.cvtColor(first_frame, cv2.COLOR_BGR2GRAY)

    # ── Load RF / SGD / ensemble (identical to realtime_monitor.py) ──────────
    _models = _load_rf_models()
    if _models:
        state_clf, beh_clf, feat_meta = _models
        zone_to_int   = feat_meta["zone_to_int"]
        state_uses_qs = "quiet_streak" in feat_meta.get("state_features", [])
        logger.info(f"Mode      : RF classifiers  ({STATE_MODEL_PATH}, {BEHAVIOR_MODEL_PATH})")
        logger.info(f"            state quiet_streak feature: {state_uses_qs}")
    else:
        state_clf = beh_clf = zone_to_int = None
        state_uses_qs = False
        logger.warning("Mode      : threshold-based fallback")

    sgd_clf = None; sgd_is_fitted = False
    if os.path.exists(SGD_MODEL_PATH):
        try:
            sgd_clf       = joblib.load(SGD_MODEL_PATH)
            sgd_is_fitted = True
            logger.info(f"SGD model : {SGD_MODEL_PATH}  (online student active)")
        except Exception as _e:
            logger.warning(f"SGD model : load failed ({_e}) — disabled")

    sgd_v2_clf = None; rf_class_map = None; sgd_v2_class_map = None
    ens_rf_weight = 0.7; ens_sgd_weight = 0.3
    if os.path.exists(ENSEMBLE_CONFIG_PATH) and state_clf is not None:
        try:
            with open(ENSEMBLE_CONFIG_PATH) as _f:
                _ecfg = json.load(_f)
            ens_rf_weight  = _ecfg.get("rf_weight",  0.7)
            ens_sgd_weight = _ecfg.get("sgd_weight", 0.3)
            if os.path.exists(SGD_V2_MODEL_PATH):
                sgd_v2_clf       = joblib.load(SGD_V2_MODEL_PATH)
                _rf_cls          = list(state_clf.classes_)
                _v2_cls          = list(sgd_v2_clf.classes_)
                rf_class_map     = [_rf_cls.index(c) for c in _ALL_STATE_CLASSES]
                sgd_v2_class_map = [_v2_cls.index(c) for c in _ALL_STATE_CLASSES]
                logger.info(f"Ensemble  : RF x{ens_rf_weight} + SGD_v2 x{ens_sgd_weight}  "
                            f"({SGD_V2_MODEL_PATH})")
        except Exception as _e:
            logger.warning(f"Ensemble  : load failed ({_e}) — disabled")

    v3_clf = None; v3_feat_names = None
    if os.path.exists(V3_MODEL_PATH) and os.path.exists(V3_FEATURES_PATH):
        try:
            v3_clf = joblib.load(V3_MODEL_PATH)
            with open(V3_FEATURES_PATH) as _f:
                _v3meta = json.load(_f)
            v3_feat_names = _v3meta["state_features_v3"]
            logger.info(f"RF v3     : {V3_MODEL_PATH}  "
                        f"({len(v3_feat_names)} features, temporal window={TEMPORAL_WINDOW})")
        except Exception as _e:
            logger.warning(f"RF v3     : load failed ({_e}) — using v1 fallback")

    logger.info(f"Detection : {'YOLO' if use_yolo else 'motion detection (fallback)'}")
    logger.info(f"Video     : {video_path}")
    logger.info(f"OCR start : '{ts0}'  ->  clock anchor {base_hms}")
    logger.info(f"FRAME_SKIP: {FRAME_SKIP}  (effective {FPS/FRAME_SKIP:.1f} Hz)")
    logger.info(f"Status every {STATUS_INTERVAL_FRAMES} video frames  |  "
                f"Summary every {SUMMARY_INTERVAL_FRAMES} video frames")
    logger.info("-" * 72)

    # ── Rolling deques ────────────────────────────────────────────────────────
    pos_deque    = deque(maxlen=SMOOTH_WINDOW * 2 + 1)
    move_deque   = deque(maxlen=ACTIVITY_SMOOTH_WINDOW)
    stereo_deque = deque(maxlen=STEREO_WINDOW_FRAMES)
    area_deque   = deque(maxlen=ACTIVITY_SMOOTH_WINDOW)

    interval_states    = []
    interval_zones     = []
    interval_behaviors = []
    interval_mice      = []   # mice-detected count per decoded frame

    total_distance   = 0.0
    total_decoded    = 0
    session_active   = 0
    session_inactive = 0

    state = rf_state = ens_state = "Wake / Active"
    v3_state = smoothed_state = "Wake / Active"
    zone      = "Center"
    behavior  = "Normal"
    prev_smooth     = None
    quiet_streak    = 0
    _yolo_boxes_cur = []   # list of (x1,y1,x2,y2,conf) for current frame display

    state_history = deque(maxlen=CONSECUTIVE_QUIET_FOR_SLEEP + 1)
    _qs_win_deque = deque(maxlen=TEMPORAL_WINDOW)
    _ia_win_deque = deque(maxlen=TEMPORAL_WINDOW)

    sgd_state        = "-"
    sgd_feat_buf     = []
    sgd_window_count = 0

    min_sleep_decoded = int(POSSIBLE_SLEEP_MIN_SECONDS * FPS / FRAME_SKIP)
    frame_index   = 1
    last_status_at  = 0
    last_summary_at = 0
    running = True

    def _stop(sig, _):
        nonlocal running
        logger.info("Interrupt — flushing and closing...")
        running = False
    signal.signal(signal.SIGINT, _stop)

    csv_fh = open(REALTIME_SUMMARY_PATH, "w", newline="")
    writer = csv.writer(csv_fh)
    writer.writerow(SUMMARY_HEADER)

    try:
        while running:
            # ── Frame skip ────────────────────────────────────────────────────
            for _ in range(FRAME_SKIP - 1):
                if not cap.grab():
                    running = False
                    break
            if not running:
                break

            ret, frame = cap.read()
            if not ret:
                break

            frame_index  += FRAME_SKIP
            total_decoded += 1

            if max_video_frames and frame_index > max_video_frames:
                break

            # ── Grayscale + ROI (used for both motion_area and fallback) ──────
            gray     = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, _w    = gray.shape
            roi_y    = int(h * ROI_TOP_FRACTION)
            roi_prev = prev_gray[roi_y:, :]
            roi_curr = gray[roi_y:, :]

            # Always compute frame diff to keep motion_area feature in-distribution
            # for the RF classifier (which was trained on contour-based motion_area)
            diff  = cv2.absdiff(roi_prev, roi_curr)
            blur  = cv2.GaussianBlur(diff, BLUR_KERNEL, 0)
            _, thresh = cv2.threshold(blur, MOTION_THRESHOLD, 255, cv2.THRESH_BINARY)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                            cv2.CHAIN_APPROX_SIMPLE)
            prev_gray = gray.copy()

            motion_area = 0.0
            if contours:
                largest     = max(contours, key=cv2.contourArea)
                motion_area = cv2.contourArea(largest)

            # ── Detection: YOLO or motion fallback ───────────────────────────
            detected        = False
            _yolo_boxes_cur = []

            if use_yolo:
                detections = _yolo_detect(yolo_model, frame, YOLO_CONF_THRESHOLD)
                if detections:
                    detected        = True
                    _yolo_boxes_cur = detections
                    # Primary subject = largest box (index 0 after sort)
                    x1, y1, x2, y2, _conf = detections[0]
                    cx = (x1 + x2) // 2
                    cy = (y1 + y2) // 2
                    pos_deque.append((cx, cy))
                    # motion_area from frame diff is already computed above
                    if motion_area < MIN_CONTOUR_AREA:
                        # YOLO found a mouse but no motion diff — use box area as proxy
                        motion_area = float((x2 - x1) * (y2 - y1)) * 0.1
            else:
                # Fallback: original motion detection
                if contours and motion_area > MIN_CONTOUR_AREA:
                    largest = max(contours, key=cv2.contourArea)
                    x, y, bw, bh = cv2.boundingRect(largest)
                    pos_deque.append((x + bw // 2, roi_y + y + bh // 2))
                    detected = True

            mice_count = len(_yolo_boxes_cur) if use_yolo else (1 if detected else 0)
            interval_mice.append(mice_count)

            if not detected:
                move_deque.append(0.0)
                area_deque.append(0.0)
                session_inactive += 1
            else:
                sx, sy = smooth_positions(list(pos_deque), SMOOTH_WINDOW)[-1]

                dist = (math.sqrt((sx - prev_smooth[0])**2 + (sy - prev_smooth[1])**2)
                        if prev_smooth else 0.0)
                prev_smooth     = (sx, sy)
                total_distance += dist
                move_deque.append(dist)
                area_deque.append(motion_area)

                movement_smooth = float(np.mean(move_deque))
                rf_ready        = (len(move_deque) == ACTIVITY_SMOOTH_WINDOW
                                   and len(area_deque) == ACTIVITY_SMOOTH_WINDOW)

                if movement_smooth < WAKE_THRESHOLD:
                    quiet_streak += 1
                else:
                    quiet_streak = 0

                # ── Build RF feature vectors (identical to realtime_monitor.py) ─
                if rf_ready:
                    _zone_int = zone_to_int.get(zone, 4) if zone_to_int else 4
                    _common   = [
                        dist,
                        movement_smooth,
                        int(movement_smooth < SMOOTH_INACTIVITY_THRESHOLD),
                        motion_area,
                        float(np.mean(move_deque)),
                        float(np.std(move_deque)),
                        float(np.mean(area_deque)),
                        _zone_int,
                    ]
                    _state_feat = np.array(
                        [_common + [float(quiet_streak)]] if state_uses_qs else [_common],
                        dtype=np.float32,
                    )
                    _beh_feat = np.array([_common], dtype=np.float32)
                    _dm = _common[4]
                    _ds = _common[5]
                    _v2_feat = np.array([[
                        *_common,
                        float(quiet_streak),
                        dist * motion_area,
                        _ds / (_dm + 1e-6),
                        float(quiet_streak) ** 2,
                        movement_smooth ** 2,
                    ]], dtype=np.float32)

                # ── RF v1 state classification ────────────────────────────────
                if state_clf is not None and rf_ready:
                    rf_state = state_clf.predict(_state_feat)[0]
                else:
                    if quiet_streak >= min_sleep_decoded:
                        rf_state = "Possible Sleep"
                    elif movement_smooth < WAKE_THRESHOLD:
                        rf_state = "Quiet / Rest"
                    else:
                        rf_state = "Wake / Active"

                # ── Ensemble: RF×0.7 + SGD_v2×0.3 ───────────────────────────
                if (sgd_v2_clf is not None and rf_class_map is not None
                        and state_clf is not None and rf_ready):
                    _rf_p  = state_clf.predict_proba(_state_feat)[0][rf_class_map]
                    _v2_p  = sgd_v2_clf.predict_proba(_v2_feat)[0][sgd_v2_class_map]
                    _ens_p = ens_rf_weight * _rf_p + ens_sgd_weight * _v2_p
                    ens_state = _ALL_STATE_CLASSES[int(np.argmax(_ens_p))]
                else:
                    ens_state = rf_state

                state = ens_state

                # ── RF v3 temporal features ───────────────────────────────────
                _qs_win_deque.append(float(quiet_streak))
                _ia_win_deque.append(int(movement_smooth < SMOOTH_INACTIVITY_THRESHOLD))
                if v3_clf is not None and rf_ready:
                    _max_qs     = max(_qs_win_deque)
                    _pct_ia     = sum(_ia_win_deque) / len(_ia_win_deque)
                    _ia_list    = list(_ia_win_deque)
                    _transitions = float(sum(
                        abs(_ia_list[i] - _ia_list[i-1])
                        for i in range(1, len(_ia_list))
                    ))
                    _v3_feat = np.array([[
                        *_common,
                        float(quiet_streak),
                        dist * motion_area,
                        _ds / (_dm + 1e-6),
                        float(quiet_streak) ** 2,
                        movement_smooth ** 2,
                        _max_qs,
                        _pct_ia,
                        _transitions,
                    ]], dtype=np.float32)
                    v3_state = v3_clf.predict(_v3_feat)[0]
                else:
                    v3_state = rf_state

                if state == "Wake / Active":
                    session_active   += 1
                else:
                    session_inactive += 1

                # ── SGD v1 online student ─────────────────────────────────────
                if sgd_clf is not None and rf_ready:
                    if sgd_is_fitted:
                        sgd_state = sgd_clf.predict(_state_feat)[0]
                    sgd_feat_buf.append((_state_feat[0].copy(), rf_state))

                # ── Zone ──────────────────────────────────────────────────────
                zone = _zone_for_point(sx, sy)

                # ── Behavior ──────────────────────────────────────────────────
                stereo_deque.append((sx, sy))
                if beh_clf is not None and rf_ready:
                    behavior = beh_clf.predict(_beh_feat)[0]
                elif len(stereo_deque) >= STEREO_WINDOW_FRAMES // 2:
                    xs_ = [p[0] for p in stereo_deque]
                    ys_ = [p[1] for p in stereo_deque]
                    spread       = math.sqrt(float(np.std(xs_))**2 + float(np.std(ys_))**2)
                    is_moving    = movement_smooth >= WAKE_THRESHOLD
                    not_sleeping = state != "Possible Sleep"
                    behavior     = ("Repetitive"
                                    if spread < STEREO_SPATIAL_RADIUS_PX
                                    and is_moving and not_sleeping
                                    else "Normal")
                else:
                    behavior = "Normal"

            # ── SGD partial_fit ───────────────────────────────────────────────
            if sgd_clf is not None and len(sgd_feat_buf) >= ONLINE_WINDOW_SIZE:
                X_buf = np.array([r[0] for r in sgd_feat_buf], dtype=np.float32)
                y_buf = [r[1] for r in sgd_feat_buf]
                sgd_clf.partial_fit(X_buf, y_buf, classes=_ALL_STATE_CLASSES)
                sgd_is_fitted    = True
                sgd_window_count += 1
                sgd_feat_buf.clear()
                logger.debug(f"[SGD] partial_fit update #{sgd_window_count}")

            interval_states.append(state)
            interval_zones.append(zone)
            interval_behaviors.append(behavior)

            # ── Live display ──────────────────────────────────────────────────
            display_frame = frame.copy()
            _dcolor = _STATE_COLORS.get(v3_state, (255, 255, 255))
            _active_pct = session_active / max(total_decoded, 1) * 100

            if use_yolo and _yolo_boxes_cur:
                _draw_yolo_boxes(display_frame, _yolo_boxes_cur, v3_state)
            elif not use_yolo and detected:
                # Motion-fallback: draw contour rect
                largest = max(contours, key=cv2.contourArea) if contours else None
                if largest is not None:
                    x, y, bw, bh = cv2.boundingRect(largest)
                    cv2.rectangle(display_frame,
                                  (x, roi_y+y), (x+bw, roi_y+y+bh), _dcolor, 2)

            # Bottom-left overlay (same layout as realtime_monitor.py)
            _put_text_bg(display_frame, f"Mice   : {mice_count}",              (10, 30), _dcolor)
            _put_text_bg(display_frame, f"State  : {smoothed_state}",         (10, 55), _dcolor)
            _put_text_bg(display_frame, f"Zone   : {zone}",                   (10, 80), (255, 255, 255))
            _put_text_bg(display_frame, f"Dist   : {int(total_distance)} px", (10,105), (255, 255, 255))
            _put_text_bg(display_frame, f"Active : {round(_active_pct,1)}%",  (10,130), (255, 255, 255))
            try:
                cv2.imshow("IVC Cage — YOLO Behavioral Monitor", display_frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    running = False
            except cv2.error:
                pass
            # Save one screenshot at ~5 seconds in (60 decoded frames at 12.5 Hz)
            if total_decoded == 60:
                cv2.imwrite("outputs/yolo_ui_final.jpg", display_frame)
                print("Screenshot saved to outputs/yolo_ui_final.jpg")

            # ── Status print ──────────────────────────────────────────────────
            if frame_index - last_status_at >= STATUS_INTERVAL_FRAMES:
                last_status_at = frame_index
                active_pct = session_active / max(total_decoded, 1) * 100
                clock = _clock_str(base_hms, frame_index)
                state_history.append(v3_state)
                smoothed_state = smooth_state_predictions(list(state_history))[-1]
                logger.info(
                    f"[{clock}]  Mice:{mice_count}  RF-v3:{v3_state:<14} | "
                    f"SGD:{sgd_state:<14} | Ens:{ens_state:<14} | "
                    f"Smoothed:{smoothed_state:<14} | {zone:<11} | "
                    f"{round(total_distance):>8,} px | active {active_pct:5.1f}%"
                )

            # ── Summary CSV row ───────────────────────────────────────────────
            if frame_index - last_summary_at >= SUMMARY_INTERVAL_FRAMES:
                last_summary_at = frame_index
                _flush_summary_row(writer, csv_fh,
                                   _clock_str(base_hms, frame_index),
                                   interval_states, interval_zones,
                                   interval_behaviors, total_distance,
                                   interval_mice)
                interval_states.clear()
                interval_zones.clear()
                interval_behaviors.clear()
                interval_mice.clear()

    finally:
        if interval_states:
            _flush_summary_row(writer, csv_fh,
                               _clock_str(base_hms, frame_index),
                               interval_states, interval_zones,
                               interval_behaviors, total_distance,
                               interval_mice)
        csv_fh.close()
        cv2.destroyAllWindows()
        cap.release()

        if sgd_clf is not None:
            try:
                joblib.dump(sgd_clf, SGD_MODEL_PATH, compress=3)
                logger.info(f"SGD model saved: {SGD_MODEL_PATH}  "
                            f"({sgd_window_count} update(s) this session)")
            except Exception as _e:
                logger.warning(f"SGD save failed: {_e}")

        active_pct = session_active / max(total_decoded, 1) * 100
        logger.info("-" * 72)
        logger.info(f"Stopped at video frame {frame_index:,}  "
                    f"({total_decoded:,} decoded frames processed)")
        logger.info(f"Total distance : {round(total_distance):,} px")
        logger.info(f"Active         : {session_active:,} / {total_decoded:,} "
                    f"decoded frames  ({active_pct:.1f}%)")
        logger.info(f"Summary CSV    : {REALTIME_SUMMARY_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real-time behavioral monitor — YOLO detection variant")
    parser.add_argument("--video",      default=VIDEO_PATH,
                        help="Video file or camera index (default: %(default)s)")
    parser.add_argument("--max-frames", type=int, default=None,
                        help="Stop after this many video frames (smoke test)")
    args = parser.parse_args()
    run(video_path=args.video, max_video_frames=args.max_frames)
