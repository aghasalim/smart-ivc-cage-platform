"""
IVC Live Mouse Detection — Paste this into a new cell in your Colab notebook.

What it does:
  1. Loads your trained best.pt (YOLOv8)
  2. Fetches JPEG frames from the Pi camera via cam.example.org/snapshot/<cam_id>
  3. Runs inference on each frame
  4. POSTs the detection results to example.org/api/v1/ai/detections
  5. The Cameras page on the dashboard shows results live with bounding-box overlay

Prerequisites:
  - best.pt already trained (your existing Colab training output ✓)
  - API key from: example.org → Settings → API Keys → Create new key

Usage:
  - Paste this into a new cell in your Colab notebook
  - Fill in API_KEY below
  - Run the cell → the dashboard goes live immediately
  - Press the Stop button (■) in Colab to pause
"""

# ─── CONFIG — fill in your API key ──────────────────────────────────────────
API_KEY      = "ivc_REPLACE_WITH_YOUR_KEY"   # Settings → API Keys
BACKEND_URL  = "https://example.org"
CAMERA_URL   = "https://cam.example.org"     # Pi camera service (public tunnel)
MODEL_PATH   = "/content/runs/mouse_detector/weights/best.pt"  # your trained weights
CONF_THRESH  = 0.40   # detection confidence threshold (0–1)
PUSH_EVERY_N = 1      # push to dashboard every N frames (1 = every frame)
SLEEP_S      = 0.5    # seconds between frames (~2 fps to the dashboard)
# ─────────────────────────────────────────────────────────────────────────────

import time
import io
import requests
import numpy as np
import cv2
from IPython.display import display, clear_output
import ipywidgets as widgets

# Load YOLOv8 model (ultralytics already installed in your notebook)
from ultralytics import YOLO

print("Loading YOLOv8 model from", MODEL_PATH, "...")
model = YOLO(MODEL_PATH)
print("Model loaded ✓  (73 layers, 3M params)")

# Auth header — API key is passed as X-API-Key
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

# Discover available cameras from the Pi
def get_cameras():
    try:
        r = requests.get(f"{CAMERA_URL}/api/cameras", timeout=5)
        r.raise_for_status()
        return r.json().get("cameras", [])
    except Exception as e:
        print(f"⚠ Could not list cameras: {e}")
        return []

cameras = get_cameras()
if cameras:
    CAM_ID = cameras[0]["id"]
    print(f"Using camera: {cameras[0]['name']}  (id={CAM_ID})")
    FRAME_W = cameras[0].get("width", 640)
    FRAME_H = cameras[0].get("height", 480)
else:
    CAM_ID  = "0"
    FRAME_W = 640
    FRAME_H = 480
    print("⚠ No cameras found — will still push empty frames so the dashboard shows live status")

SNAPSHOT_URL = f"{CAMERA_URL}/snapshot/{CAM_ID}"

def fetch_frame() -> np.ndarray | None:
    """Download one JPEG snapshot from the Pi camera."""
    try:
        r = requests.get(SNAPSHOT_URL, timeout=4, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        arr = np.frombuffer(r.content, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        print(f"⚠ Frame fetch failed: {e}")
        return None

def push_detections(count, detections, inference_ms, frame_w, frame_h):
    """POST detection results to the dashboard backend."""
    payload = {
        "count": count,
        "detections": detections,
        "source": "colab_yolov8",
        "model_version": "yolov8",
        "cam_id": CAM_ID,
        "frame_width": frame_w,
        "frame_height": frame_h,
        "inference_ms": round(inference_ms, 2),
    }
    try:
        r = requests.post(
            f"{BACKEND_URL}/api/v1/ai/detections",
            json=payload,
            headers=HEADERS,
            timeout=3,
        )
        if r.status_code == 401:
            print("✗ AUTH FAILED — check your API key in Settings → API Keys")
            return False
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"⚠ Push failed: {e}")
        return False

# ── Preview widget (shows annotated frames inline in Colab) ──────────────────
preview_img = widgets.Image(format="jpeg", width=480)
status_lbl  = widgets.Label(value="Starting…")
display(widgets.VBox([status_lbl, preview_img]))

frame_n   = 0
push_ok   = 0
push_fail = 0

print("\n🟢 Live inference running — check the Cameras page on example.org")
print("   Press ■ Stop to pause.\n")

try:
    while True:
        frame_n += 1
        t0 = time.perf_counter()

        # 1. Grab frame from Pi camera
        img = fetch_frame()
        if img is None:
            time.sleep(1)
            continue

        h, w = img.shape[:2]

        # 2. Run YOLOv8 inference
        t_inf = time.perf_counter()
        results = model(img, conf=CONF_THRESH, verbose=False)
        inference_ms = (time.perf_counter() - t_inf) * 1000

        # 3. Parse detections
        detections = []
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                detections.append({
                    "confidence": float(box.conf[0]),
                    "class_id":   cls_id,
                    "class_name": model.names.get(cls_id, "mouse"),
                    "bbox":       [round(v, 1) for v in box.xyxy[0].tolist()],
                })

        count = len(detections)

        # 4. Push to dashboard
        if frame_n % PUSH_EVERY_N == 0:
            ok = push_detections(count, detections, inference_ms, w, h)
            if ok:
                push_ok += 1
            else:
                push_fail += 1

        # 5. Draw bounding boxes for the Colab preview
        vis = img.copy()
        for det in detections:
            x1, y1, x2, y2 = [int(v) for v in det["bbox"]]
            cv2.rectangle(vis, (x1, y1), (x2, y2), (56, 189, 248), 2)
            label = f"{det['class_name']} {round(det['confidence']*100)}%"
            cv2.putText(vis, label, (x1, max(y1 - 6, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (56, 189, 248), 1, cv2.LINE_AA)

        # Update Colab inline preview
        _, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 75])
        preview_img.value = buf.tobytes()

        elapsed = (time.perf_counter() - t0) * 1000
        status_lbl.value = (
            f"Frame {frame_n} | Mice: {count} | "
            f"Inference: {inference_ms:.1f}ms | "
            f"Total: {elapsed:.0f}ms | "
            f"Pushes OK/Fail: {push_ok}/{push_fail}"
        )

        # Throttle to ~2 fps to the dashboard (Pi snapshot rate)
        sleep = max(0, SLEEP_S - (time.perf_counter() - t0))
        time.sleep(sleep)

except KeyboardInterrupt:
    print(f"\n⏹ Stopped after {frame_n} frames  ({push_ok} pushes OK, {push_fail} failed)")
