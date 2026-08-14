"""
IVC Cage — Google Colab mouse detector (GPU)
============================================

Runs Fine_Tuned.pt on Colab's free T4 GPU (~10 ms/frame), pulls live snapshots
from all Pi cameras, draws bounding boxes, and POSTs annotated frames to the
dashboard's Live AI Detection panel — exactly the same endpoint the on-Pi
inference uses, so nothing on the backend/frontend changes.

HOW TO RUN (paste each CELL below into a separate Colab cell):
  1. Runtime → Change runtime type → Hardware accelerator → T4 GPU
  2. Run CELL 1 (install)
  3. Run CELL 2 and upload Fine_Tuned.pt when prompted
  4. Put your API key in CELL 3, then run CELL 3 (the live loop)

The loop runs until you press Stop. Watch example.org → Cameras → Live AI Detection.
"""

# ════════════════════════════════════════════════════════════════════════
# CELL 1 — install dependencies
# ════════════════════════════════════════════════════════════════════════
# !pip install -q ultralytics requests


# ════════════════════════════════════════════════════════════════════════
# CELL 2 — upload the model
# ════════════════════════════════════════════════════════════════════════
# from google.colab import files
# print("Upload Fine_Tuned.pt:")
# files.upload()          # pick Fine_Tuned.pt from your computer
# # (Alternatively, mount Drive and point MODEL_PATH at it.)


# ════════════════════════════════════════════════════════════════════════
# CELL 3 — the live detection loop
# ════════════════════════════════════════════════════════════════════════
import base64
import time
import cv2
import numpy as np
import requests
from ultralytics import YOLO

# ── CONFIG — set your API key ────────────────────────────────────────────
API_KEY     = "ivc_REPLACE_WITH_YOUR_KEY"        # Settings → API Keys → Create
MODEL_PATH  = "Fine_Tuned.pt"                     # uploaded in CELL 2
CAMERA_URL  = "https://cam.example.org"           # Pi camera stream (public)
BACKEND_URL = "https://example.org"               # dashboard backend
CONF        = 0.40                                # confidence threshold
IMGSZ       = 640                                 # GPU is fast — use full res
JPEG_QUALITY = 80

# ── Load model onto the GPU ──────────────────────────────────────────────
model = YOLO(MODEL_PATH)
print("Loaded model — classes:", model.names)

ACCENT = (16, 185, 129)  # green, matches the dashboard


def list_cameras():
    r = requests.get(f"{CAMERA_URL}/api/cameras", timeout=8)
    return r.json().get("cameras", [])


def fetch_frame(cam_id):
    r = requests.get(f"{CAMERA_URL}/snapshot/{cam_id}", timeout=6)
    if r.status_code != 200:
        return None
    arr = np.frombuffer(r.content, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def annotate(img, dets):
    vis = img.copy()
    for d in dets:
        x1, y1, x2, y2 = [int(v) for v in d["bbox"]]
        cv2.rectangle(vis, (x1, y1), (x2, y2), ACCENT, 3)
        label = f"{d['class_name']} {round(d['confidence'] * 100)}%"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        ly = max(y1 - th - 8, 0)
        cv2.rectangle(vis, (x1, ly), (x1 + tw + 10, ly + th + 8), ACCENT, -1)
        cv2.putText(vis, label, (x1 + 5, ly + th + 1),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
    n = len(dets)
    badge = f"{n} mouse" if n == 1 else f"{n} mice"
    cv2.rectangle(vis, (8, 8), (8 + 14 * len(badge) + 12, 40),
                  ACCENT if n else (90, 90, 90), -1)
    cv2.putText(vis, badge, (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (0, 0, 0) if n else (220, 220, 220), 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes() if ok else b""


def infer(img):
    t0 = time.perf_counter()
    res = model.predict(img, imgsz=IMGSZ, conf=CONF, verbose=False)[0]
    ms = (time.perf_counter() - t0) * 1000
    names = res.names if isinstance(res.names, dict) else dict(enumerate(res.names))
    dets = []
    for b in res.boxes:
        cls = int(b.cls[0])
        x1, y1, x2, y2 = b.xyxy[0].tolist()
        dets.append({
            "confidence": float(b.conf[0]),
            "class_id": cls,
            "class_name": names.get(cls, "Mouse"),
            "bbox": [round(float(v), 1) for v in (x1, y1, x2, y2)],
        })
    return dets, ms


def push(cam_id, dets, ms, w, h, jpeg):
    payload = {
        "count": len(dets),
        "detections": dets,
        "source": "colab_gpu_yolov8",
        "model_version": "Fine_Tuned.pt",
        "cam_id": str(cam_id),
        "frame_width": w,
        "frame_height": h,
        "inference_ms": round(ms, 2),
        "frame_b64": base64.b64encode(jpeg).decode() if jpeg else None,
    }
    try:
        r = requests.post(f"{BACKEND_URL}/api/v1/ai/detections", json=payload,
                          headers={"X-API-Key": API_KEY}, timeout=10)
        return r.status_code
    except Exception as e:
        print("POST failed:", e)
        return 0


# ── Main loop ────────────────────────────────────────────────────────────
assert API_KEY.startswith("ivc_"), "Set API_KEY first!"
cams = list_cameras()
print("Cameras:", [(c["id"], c.get("name", "")) for c in cams])

frame_no = 0
while True:
    for cam in cams:
        img = fetch_frame(cam["id"])
        if img is None:
            continue
        h, w = img.shape[:2]
        dets, ms = infer(img)
        jpeg = annotate(img, dets)
        code = push(cam["id"], dets, ms, w, h, jpeg)
        frame_no += 1
        print(f"frame={frame_no} cam={cam['id']} mice={len(dets)} "
              f"infer={ms:.0f}ms post={code}")
    time.sleep(0.1)  # gentle pacing; remove for max fps
