"""
IVC Cage — Colab: YOLO detection + EXACT-feature behaviour/state classification
================================================================================

Now uses the REAL training features from feature_columns*.json (uploaded):
  state_classifier_v3.pkl ← state_features_v3 (16 features)
  behavior_classifier.pkl ← behavior_features  (8 features)

Per camera we track the mouse's centroid/box and compute the named features
(distance_from_previous_px, movement_smooth, motion_area, rolling means/stds over
15 frames, zone_enc on a 3×3 grid, quiet_streak, temporal-window stats over 450
frames, …) IN THE EXACT ORDER the JSON specifies, then feed the classifiers.

SETUP (separate cells):
  CELL 0:  !pip install -q ultralytics requests scikit-learn==1.8.0
           # Runtime → Restart session afterwards
  CELL 1:  upload Fine_Tuned.pt, behavior_classifier.pkl, state_classifier_v3.pkl,
           feature_columns.json, feature_columns_v3.json
  CELL 2:  set API_KEY below and run this file

Two tunables (movement smoothing + inactivity threshold) are flagged TUNE — if
labels look off, those are the knobs.
"""
import base64, json, pickle, time, warnings, collections, math
import cv2, numpy as np, requests
from ultralytics import YOLO
warnings.filterwarnings("ignore")

# ── CONFIG ───────────────────────────────────────────────────────────────
API_KEY      = "ivc_UO9RdaQIPtGDgMAhIAP4NQg8gkVoO8VCgxXKfQE8WAc"
YOLO_PATH    = "Fine_Tuned.pt"
STATE_PKL    = "state_classifier_v3.pkl"
BEHAV_PKL    = "behavior_classifier.pkl"
COLS_V3      = "feature_columns_v3.json"     # has state_features_v3 (16)
COLS_BASE    = "feature_columns.json"        # has behavior_features (8) + zone map
CAMERA_URL   = "https://cam.example.org"
BACKEND_URL  = "https://example.org"
CONF, IMGSZ  = 0.40, 640

EMA_ALPHA      = 0.3     # TUNE: movement_smooth smoothing factor
INACTIVE_THRESH = 5.0    # TUNE: px of smoothed movement below which "inactive"
ROLL = 15                # rolling_window  (from JSON)
TEMPORAL = 450           # temporal_window (from JSON)

# ── Load models + feature specs ──────────────────────────────────────────
yolo = YOLO(YOLO_PATH)
def _load(p):
    o = pickle.load(open(p, "rb"))
    return (o.get("model") or o.get("clf") or o.get("classifier")) if isinstance(o, dict) else o
state_clf = _load(STATE_PKL)
behav_clf = _load(BEHAV_PKL)

_v3   = json.load(open(COLS_V3))
_base = json.load(open(COLS_BASE))
STATE_COLS = _v3["state_features_v3"]            # 16, exact order
BEHAV_COLS = _base["behavior_features"]          # 8, exact order
ZONE_MAP   = _base.get("zone_to_int", {})
ROLL       = _base.get("rolling_window", ROLL)
TEMPORAL   = _v3.get("temporal_window", TEMPORAL)

print("YOLO classes :", yolo.names)
print("state classes:", list(state_clf.classes_), "| needs", state_clf.n_features_in_, "feats; have", len(STATE_COLS))
print("behav classes:", list(behav_clf.classes_), "| needs", behav_clf.n_features_in_, "feats; have", len(BEHAV_COLS))


# ── Per-camera feature tracker ────────────────────────────────────────────
def zone_enc(cx, cy, W, H):
    """3×3 grid → 0..8 matching {TopLeft:0 ... BotRight:8}."""
    col = min(int(cx / max(W, 1) * 3), 2)
    row = min(int(cy / max(H, 1) * 3), 2)
    names = [["TopLeft","TopCenter","TopRight"],
             ["MidLeft","Center","MidRight"],
             ["BotLeft","BotCenter","BotRight"]]
    return ZONE_MAP.get(names[row][col], row * 3 + col)


class CamTrack:
    def __init__(self):
        self.prev = None              # (cx, cy)
        self.ema = 0.0                # movement_smooth
        self.quiet = 0                # current quiet_streak
        self.last_zone = 4
        self.dist15  = collections.deque(maxlen=ROLL)
        self.area15  = collections.deque(maxlen=ROLL)
        self.inact_T = collections.deque(maxlen=TEMPORAL)
        self.quiet_T = collections.deque(maxlen=TEMPORAL)
        self.n = 0

    def update(self, det, W, H):
        """det = {'bbox':[x1,y1,x2,y2]} or None. Returns dict of all features."""
        if det:
            x1, y1, x2, y2 = det["bbox"]
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            area = max((x2 - x1) * (y2 - y1), 0.0)
            self.last_zone = zone_enc(cx, cy, W, H)
        else:
            cx = cy = None
            area = 0.0

        if det and self.prev is not None:
            dist = math.hypot(cx - self.prev[0], cy - self.prev[1])
        else:
            dist = 0.0
        if det:
            self.prev = (cx, cy)

        self.ema = EMA_ALPHA * dist + (1 - EMA_ALPHA) * self.ema   # movement_smooth
        is_inactive = 1 if self.ema < INACTIVE_THRESH else 0
        self.quiet = self.quiet + 1 if is_inactive else 0

        self.dist15.append(dist)
        self.area15.append(area)
        self.inact_T.append(is_inactive)
        self.quiet_T.append(self.quiet)
        self.n += 1

        d = np.array(self.dist15, float)
        a = np.array(self.area15, float)
        dist_roll_mean = float(d.mean())
        dist_roll_std  = float(d.std())
        area_roll_mean = float(a.mean())
        cv_movement = dist_roll_std / (dist_roll_mean + 1e-6)
        inact = np.array(self.inact_T, int)
        transitions = int(np.abs(np.diff(inact)).sum()) if len(inact) > 1 else 0

        f = {
            "distance_from_previous_px": dist,
            "movement_smooth": self.ema,
            "is_inactive_smooth_int": is_inactive,
            "motion_area": area,
            "dist_roll_mean_15": dist_roll_mean,
            "dist_roll_std_15": dist_roll_std,
            "area_roll_mean_15": area_roll_mean,
            "zone_enc": self.last_zone,
            "quiet_streak": self.quiet,
            "distance_x_motion_area": dist * area,
            "cv_movement": cv_movement,
            "quiet_streak_sq": self.quiet ** 2,
            "movement_smooth_sq": self.ema ** 2,
            "max_quiet_streak_in_window": int(max(self.quiet_T)) if self.quiet_T else 0,
            "pct_inactive_in_window": float(inact.mean()) if len(inact) else 0.0,
            "activity_transitions": transitions,
        }
        return f

    def ready(self):
        return self.n >= ROLL     # rolling features valid only after the window fills


tracks = collections.defaultdict(CamTrack)


def classify(cam, feats, tr):
    if not tr.ready():
        return None, None
    state = behav = None
    try:
        sv = np.array([[feats[c] for c in STATE_COLS]], float)
        if sv.shape[1] == state_clf.n_features_in_:
            state = str(state_clf.predict(sv)[0])
    except Exception as e:
        print("state err:", e)
    try:
        bv = np.array([[feats[c] for c in BEHAV_COLS]], float)
        if bv.shape[1] == behav_clf.n_features_in_:
            behav = str(behav_clf.predict(bv)[0])
    except Exception as e:
        print("behav err:", e)
    return state, behav


# ── Camera I/O ────────────────────────────────────────────────────────────
def list_cameras():
    try:
        return requests.get(f"{CAMERA_URL}/api/cameras", timeout=8).json().get("cameras", [])
    except Exception:
        return []

def fetch(cam_id):
    try:
        r = requests.get(f"{CAMERA_URL}/snapshot/{cam_id}", timeout=6)
        if r.status_code != 200: return None
        return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None

def detect(img):
    res = yolo.predict(img, imgsz=IMGSZ, conf=CONF, verbose=False)[0]
    dets = [{"conf": float(b.conf[0]), "bbox": b.xyxy[0].tolist()} for b in res.boxes]
    dets.sort(key=lambda d: d["conf"], reverse=True)
    return dets

def annotate(img, dets, state, behav):
    vis = img.copy(); G = (16, 185, 129)
    for i, d in enumerate(dets):
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        cv2.rectangle(vis, (x1, y1), (x2, y2), G, 3)
        parts = [f"Mouse {round(d['conf']*100)}%"]
        if i == 0 and state: parts.append(state)
        if i == 0 and behav: parts.append(behav)
        lbl = "  ·  ".join(parts)
        (tw, th), _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        ly = max(y1 - th - 8, 0)
        cv2.rectangle(vis, (x1, ly), (x1+tw+10, ly+th+8), G, -1)
        cv2.putText(vis, lbl, (x1+5, ly+th+1), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0,0,0), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else b""

def push(cam_id, dets, state, behav, ms, w, h, jpeg):
    label = "Mouse"
    if state or behav:
        label = "Mouse · " + " · ".join([x for x in (state, behav) if x])
    payload = {
        "count": len(dets),
        "detections": [{"confidence": d["conf"], "class_id": 0, "class_name": label,
                        "bbox": [round(v,1) for v in d["bbox"]]} for d in dets],
        "source": "colab_detect_classify", "model_version": "Fine_Tuned.pt+rf_v3",
        "cam_id": str(cam_id), "frame_width": w, "frame_height": h,
        "inference_ms": round(ms, 2),
        "frame_b64": base64.b64encode(jpeg).decode() if jpeg else None,
    }
    try:
        return requests.post(f"{BACKEND_URL}/api/v1/ai/detections", json=payload,
                             headers={"X-API-Key": API_KEY}, timeout=10).status_code
    except Exception as e:
        print("POST failed:", e); return 0


# ── Main loop ───────────────────────────────────────────────────────────
cams = []
for attempt in range(40):
    cams = list_cameras()
    if cams: break
    print(f"waiting for cameras... ({attempt+1})"); time.sleep(2)
print("cameras:", [(c["id"], c.get("name","")) for c in cams])

i = 0; last_relist = time.time()
while True:
    if time.time() - last_relist > 10:
        fresh = list_cameras()
        if fresh:
            if len(fresh) != len(cams): print("camera set →", [c["id"] for c in fresh])
            cams = fresh
        last_relist = time.time()
    for cam in cams:
        cid = cam["id"]
        img = fetch(cid)
        if img is None: continue
        h, w = img.shape[:2]
        t0 = time.perf_counter()
        dets = detect(img)
        ms = (time.perf_counter() - t0) * 1000
        tr = tracks[cid]
        feats = tr.update(dets[0] if dets else None, w, h)
        state, behav = classify(cid, feats, tr)
        jpeg = annotate(img, dets, state, behav)
        code = push(cid, dets, state, behav, ms, w, h, jpeg)
        i += 1
        print(f"frame={i} cam={cid} mice={len(dets)} state={state} behav={behav} infer={ms:.0f}ms post={code}")
    time.sleep(0.1)
