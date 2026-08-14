"""
On-Pi YOLOv8 mouse-detection loop — the local replacement for the Colab notebook.

Why this exists
---------------
The Colab pipeline added a full cloud round-trip per frame:
  Pi camera → Cloudflare tunnel → Colab (somewhere in Google datacentre) →
  YOLO inference → Cloudflare tunnel → example.org backend → dashboard.
End-to-end that's typically 1-3 s. Running the same model directly on the Pi
collapses all those network hops into loopback fetches, so even though Pi-CPU
inference is slower per-frame than a Colab T4 (~250 ms vs ~10 ms), the
*end-to-end* latency drops dramatically.

What it does
------------
  1. Discovers cameras from the local camera-stream service (127.0.0.1:8090).
  2. For each camera, pulls the latest JPEG snapshot, decodes it.
  3. Runs YOLOv8 (best.pt) on the frame.
  4. Draws bounding boxes onto a copy.
  5. POSTs detections + annotated JPEG (base64) to /api/v1/ai/detections so
     the dashboard's Live AI Detection panel updates in real time.

Everything except the final POST stays on loopback. The POST goes to whichever
backend URL is configured (default: the public example.org Railway backend so
the dashboard sees it; override to http://127.0.0.1:8000 for fully-offline).

Run
---
    pip install --break-system-packages -r requirements.txt
    cp env.example .env   # then edit .env with your API_KEY
    python3 inference.py

Or via systemd (ivc-inference.service in this folder).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from urllib import error, request

import cv2
import numpy as np
from ultralytics import YOLO

# Behavioural-monitoring engine handed off by the ML/CV side: YOLO detection +
# Hungarian persistent-ID tracking (A–E) + Wake/Rest/Sleep state + group social
# classification. See BACKEND_INTEGRATION_README in the handoff package.
from inference_api import BehavioralMonitor

# ── Config (env-overridable) ─────────────────────────────────────────────────
HERE         = os.path.dirname(os.path.abspath(__file__))
CAMERA_URL   = os.environ.get("CAMERA_URL",  "http://127.0.0.1:8090").rstrip("/")
BACKEND_URL  = os.environ.get("BACKEND_URL", "https://example.org").rstrip("/")
API_KEY      = os.environ.get("API_KEY", "").strip()
# Fine_Tuned.pt is the user's fine-tuned mouse detector (1 class: "Mouse").
# Falls back to best.pt if the fine-tuned file isn't present.
_DEFAULT_MODEL = os.path.join(HERE, "Fine_Tuned.pt")
if not os.path.isfile(_DEFAULT_MODEL):
    _DEFAULT_MODEL = os.path.join(HERE, "best.pt")
MODEL_PATH   = os.environ.get("MODEL_PATH",  _DEFAULT_MODEL)
# Behavioural-monitoring models (handoff). State model is REQUIRED (the
# BehavioralMonitor constructor loads it); the 85 MB stereotypy model is
# OPTIONAL and OFF by default — set BEHAVIOR_MODEL=path to enable it.
STATE_MODEL    = os.environ.get("STATE_MODEL",   os.path.join(HERE, "state_classifier_v3.pkl"))
BEHAVIOR_MODEL = os.environ.get("BEHAVIOR_MODEL", "").strip()    # "" = stereotypy off
FEATURE_COLS   = os.environ.get("FEATURE_COLS",  os.path.join(HERE, "feature_columns_v3.json"))
CONF_THRESH  = float(os.environ.get("CONF_THRESH", "0.40"))
# 480 is a sweet spot for Pi 5 + NCNN: ~60-80 ms per frame with negligible
# accuracy hit vs. 640. Drop to 320 if you need maximum fps and your mice
# fill at least ~5% of the frame; bump to 640 if you need to detect tiny ones.
YOLO_IMGSZ   = int(os.environ.get("YOLO_IMGSZ",   "480"))
# Try to use NCNN (2-3× faster than torch on ARM). Falls back to .pt if the
# ncnn package isn't installed or export fails.
USE_NCNN     = os.environ.get("USE_NCNN", "1") not in ("0", "false", "no")
# Optional pacing: if inference is faster than this, sleep the difference. 0
# means "as fast as the Pi can go" (which is what you want for live demos).
LOOP_DELAY_S = float(os.environ.get("LOOP_DELAY_S", "0"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pi-inference")


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _http_get(url: str, timeout: float = 5.0) -> bytes:
    with request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _http_post_json_resp(url: str, payload: dict, timeout: float = 10.0) -> tuple[int, dict]:
    """POST JSON; return (status_code, parsed_response_body). The body lets the
    caller read back the dashboard-selected active model from the push reply."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as r:
            try:
                return r.status, json.loads(r.read().decode() or "{}")
            except Exception:
                return r.status, {}
    except error.HTTPError as e:
        # Log auth/validation failures loudly — silent 401s are the most common
        # support issue.
        b = ""
        try:
            b = e.read().decode()[:200]
        except Exception:
            pass
        log.warning("POST %s -> %s %s | %s", url, e.code, e.reason, b)
        return e.code, {}
    except Exception as e:
        log.warning("POST %s failed: %s", url, e)
        return 0, {}


def _http_post_json(url: str, payload: dict, timeout: float = 10.0) -> int:
    """Back-compat wrapper returning just the status code."""
    status, _ = _http_post_json_resp(url, payload, timeout)
    return status


# ── Model loading ─────────────────────────────────────────────────────────────
def load_optimised_model() -> YOLO:
    """Load the fastest available model variant.

    Preference order:
      1. NCNN model directory (best_ncnn_model/) — 2-3× faster on Pi 5 ARM.
      2. Original .pt file (torch fallback).

    On first run we export .pt → NCNN automatically (one-time, ~30 s). After
    that the directory persists and subsequent boots skip straight to NCNN.

    Why NCNN: Tencent's ARM-optimised inference engine has hand-tuned NEON
    kernels for Cortex-A76 (the Pi 5's CPU). On YOLOv8n at 480 imgsz a Pi 5
    typically goes from ~250 ms/frame (torch CPU) to ~70-90 ms/frame (NCNN
    FP16) — same model weights, no retraining.
    """
    if MODEL_PATH.endswith(".pt"):
        ncnn_dir = MODEL_PATH[:-3] + "_ncnn_model"
    else:
        ncnn_dir = os.path.join(HERE, "best_ncnn_model")

    if USE_NCNN and os.path.isdir(ncnn_dir):
        log.info("loading NCNN model from %s (fast path)", ncnn_dir)
        return YOLO(ncnn_dir, task="detect")

    log.info("loading torch model %s …", MODEL_PATH)
    model = YOLO(MODEL_PATH)

    if USE_NCNN:
        log.info("NCNN model not built yet — exporting %s → NCNN (one-time, ~30s)", MODEL_PATH)
        try:
            # half=True → FP16 weights: smaller files + faster on ARM SIMD.
            model.export(format="ncnn", imgsz=YOLO_IMGSZ, half=True)
            if os.path.isdir(ncnn_dir):
                log.info("NCNN export complete — switching to %s", ncnn_dir)
                return YOLO(ncnn_dir, task="detect")
            log.warning("NCNN export ran but %s wasn't produced — keeping .pt", ncnn_dir)
        except Exception as e:
            # Most common reason: `pip install ncnn` not done yet. The .pt
            # fallback still works, just slower. Log loudly so users notice.
            log.warning("NCNN export failed (%s) — using slower .pt fallback. "
                        "Run `pip install --break-system-packages ncnn` to enable.", e)

    return model


# ── Camera + inference primitives ─────────────────────────────────────────────
def list_cameras() -> list[dict]:
    raw = _http_get(f"{CAMERA_URL}/api/cameras", timeout=5)
    return json.loads(raw).get("cameras", [])


def fetch_frame(cam_id: str) -> np.ndarray | None:
    """Fetch latest snapshot and decode to BGR ndarray."""
    try:
        raw = _http_get(f"{CAMERA_URL}/snapshot/{cam_id}", timeout=4)
    except Exception as e:
        log.warning("snapshot cam=%s failed: %s", cam_id, e)
        return None
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        log.warning("snapshot cam=%s could not be decoded", cam_id)
    return img


def run_inference(monitor: BehavioralMonitor, img: np.ndarray) -> tuple[list[dict], dict, float]:
    """Run the behavioural monitor on one frame.

    Returns (detections, group, inference_ms). Each detection stays backward-
    compatible with the backend Detection schema (confidence, class_id,
    class_name, bbox) — and additionally carries the persistent track id, the
    behavioural state, and the centroid (the backend ignores unknown fields, so
    this is safe). The per-mouse id + state are folded into class_name too so
    they show up even in plain list/JSON views, and they are drawn on the frame.
    """
    t0 = time.perf_counter()
    result = monitor.process_frame(img)
    inference_ms = (time.perf_counter() - t0) * 1000.0
    detections: list[dict] = []
    for m in result.get("mice", []):
        state = m.get("state", "")
        detections.append({
            "confidence": float(m.get("confidence", 0.0)),
            "class_id":   0,
            "class_name": f"Mouse {m.get('id', '?')} · {state}",
            "bbox":       [round(float(v), 1) for v in m.get("bbox", [0, 0, 0, 0])],
            # extra fields (ignored by the strict backend schema, used for the UI later)
            "track_id":   m.get("id"),
            "state":      state,
            "behavior":   m.get("behavior", "Normal"),
            "centroid":   m.get("centroid"),
        })
    group = result.get("group", {})
    return detections, group, inference_ms


def annotate(img: np.ndarray, detections: list[dict], group: dict | None = None,
             active_model: str = "state_v3") -> bytes:
    """Draw per-mouse boxes + a count badge + a group social-state badge; return
    JPEG bytes. The per-box label follows the dashboard-selected active model:
      state_v3   → ID · Wake/Rest/Sleep   (default)
      stereotypy → ID · Normal/Repetitive
      social     → ID only (social badge is the focus)
    Behavioural data is burned into the frame so it shows on the dashboard's
    Live AI panel without any frontend change."""
    vis = img.copy()
    GREEN  = (16, 185, 129)    # active / default accent (BGR-ish)
    AMBER  = (0, 170, 245)     # resting
    BLUE   = (235, 130, 60)    # sleeping
    for d in detections:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        state = (d.get("state") or "").lower()
        colour = BLUE if "sleep" in state else AMBER if ("rest" in state or "quiet" in state) else GREEN
        cv2.rectangle(vis, (x1, y1), (x2, y2), colour, 3)
        tid = d.get("track_id", "?")
        if active_model == "stereotypy":
            tag = d.get("behavior", "Normal")
        elif active_model == "social":
            tag = ""
        else:
            tag = d.get("state", "")
        label = (f"{tid} · {tag}  {round(d['confidence'] * 100)}%"
                 if tag else f"{tid}  {round(d['confidence'] * 100)}%")
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.52, 1)
        ly = max(y1 - th - 8, 0)
        cv2.rectangle(vis, (x1, ly), (x1 + tw + 10, ly + th + 8), colour, -1)
        cv2.putText(vis, label, (x1 + 5, ly + th + 1),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (0, 0, 0), 1, cv2.LINE_AA)
    # Count badge (top-left)
    n = len(detections)
    badge = f"{n} mouse" if n == 1 else f"{n} mice"
    cv2.rectangle(vis, (8, 8), (8 + 14 * len(badge) + 12, 40),
                  GREEN if n else (90, 90, 90), -1)
    cv2.putText(vis, badge, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (0, 0, 0) if n else (220, 220, 220), 2, cv2.LINE_AA)
    # Social-state badge (top-right) when we have a group classification
    if group and group.get("social_state") and group.get("social_state") != "Unknown":
        social = f"{group['social_state']}  ~{group.get('mean_interanimal_dist', 0):.0f}px"
        (sw, sh), _ = cv2.getTextSize(social, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        x0 = max(vis.shape[1] - sw - 24, 0)
        cv2.rectangle(vis, (x0, 8), (x0 + sw + 16, 40), (120, 70, 200), -1)
        cv2.putText(vis, social, (x0 + 8, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (255, 255, 255), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 78])
    return buf.tobytes() if ok else b""


def push(cam_id: str, detections: list[dict], group: dict, inference_ms: float,
         width: int, height: int, jpeg_bytes: bytes,
         available_models: list[dict]) -> tuple[int, str | None]:
    """POST one camera's result. Reports the available models so the dashboard
    dropdown can list them, and returns (status, active_model) — the active
    model the dashboard has selected — so the loop can switch on the next frame."""
    payload = {
        "count":         len(detections),
        "detections":    detections,
        "group":         group,
        "available_models": available_models,
        "source":        "pi_behavioral_monitor",
        "model_version": "BehavioralMonitor (YOLOv8s + state-v3 + social)",
        "cam_id":        str(cam_id),
        "frame_width":   width,
        "frame_height":  height,
        "inference_ms":  round(inference_ms, 2),
        "frame_b64":     base64.b64encode(jpeg_bytes).decode() if jpeg_bytes else None,
    }
    status, resp = _http_post_json_resp(f"{BACKEND_URL}/api/v1/ai/detections", payload)
    return status, resp.get("active_model")


def build_monitors(cam_ids: list[str]) -> dict[str, BehavioralMonitor]:
    """One BehavioralMonitor per camera so each keeps its own persistent track
    identities, but they SHARE the loaded YOLO + classifier objects so we only
    pay the (heavy) model-load cost once and keep memory in check on the Pi."""
    base = BehavioralMonitor(
        yolo_model=MODEL_PATH,
        state_model=STATE_MODEL,
        behavior_model=BEHAVIOR_MODEL or None,   # 85 MB stereotypy model: off unless set
        feature_cols=FEATURE_COLS if os.path.isfile(FEATURE_COLS) else None,
    )
    monitors: dict[str, BehavioralMonitor] = {cam_ids[0]: base}
    for cid in cam_ids[1:]:
        m = BehavioralMonitor.__new__(BehavioralMonitor)   # skip __init__ (no reload)
        m._yolo        = base._yolo
        m._state_rf    = base._state_rf
        m._behavior_rf = base._behavior_rf
        m._feature_cols = base._feature_cols
        m._tracks      = {}
        m._next_id     = 0
        monitors[cid] = m
    return monitors


# ── Main loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    if not API_KEY:
        log.error("API_KEY env var is required (Settings → API Keys → Create new key)")
        sys.exit(1)
    if not os.path.isfile(MODEL_PATH):
        log.error("MODEL_PATH not found: %s", MODEL_PATH)
        sys.exit(1)
    if not os.path.isfile(STATE_MODEL):
        log.error("STATE_MODEL not found: %s (scp state_classifier_v3.pkl to the Pi)", STATE_MODEL)
        sys.exit(1)

    # Wait up to a minute for camera-stream to come up (in case we boot first).
    cameras: list[dict] = []
    for attempt in range(30):
        try:
            cameras = list_cameras()
            if cameras:
                break
        except Exception as e:
            log.info("camera-stream not ready yet (attempt %d/30): %s", attempt + 1, e)
        time.sleep(2)
    if not cameras:
        log.error("no cameras discovered — is ivc-cameras running on :8090?")
        sys.exit(1)
    log.info("cameras: %s", [(c["id"], c["name"]) for c in cameras])

    # Build one behavioural monitor per camera (shared models, per-cam tracks).
    monitors = build_monitors([str(c["id"]) for c in cameras])
    available_models = monitors[str(cameras[0]["id"])].get_available_models()
    active_model = "state_v3"   # updated from each push response (dashboard choice)
    log.info("BehavioralMonitor ready — yolo=%s state=%s stereotypy=%s · models=%s",
             os.path.basename(MODEL_PATH), os.path.basename(STATE_MODEL),
             "on" if BEHAVIOR_MODEL else "off", [m["id"] for m in available_models])

    push_ok = 0
    push_fail = 0
    frame_no = 0

    while True:
        loop_t0 = time.perf_counter()
        for cam in cameras:
            cam_id = str(cam["id"])
            img = fetch_frame(cam_id)
            if img is None:
                continue
            h, w = img.shape[:2]
            detections, group, inf_ms = run_inference(monitors[cam_id], img)
            jpeg_bytes = annotate(img, detections, group, active_model)
            status, sel = push(cam_id, detections, group, inf_ms, w, h, jpeg_bytes,
                               available_models)
            if sel:
                active_model = sel   # honour the dashboard's model selection
            if 200 <= status < 300:
                push_ok += 1
            else:
                push_fail += 1
            frame_no += 1
            log.info("frame=%d cam=%s n=%d inf=%.1fms post=%d (ok/fail %d/%d)",
                     frame_no, cam_id, len(detections), inf_ms, status, push_ok, push_fail)

        if LOOP_DELAY_S > 0:
            elapsed = time.perf_counter() - loop_t0
            if elapsed < LOOP_DELAY_S:
                time.sleep(LOOP_DELAY_S - elapsed)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("interrupted — bye")
