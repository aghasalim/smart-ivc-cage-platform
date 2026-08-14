"""
Live AI detection endpoint — multi-camera.

Colab loops all cameras and POSTs results per camera:
  POST /api/v1/ai/detections          — push one camera's frame

Dashboard polls:
  GET  /api/v1/ai/detections/all      — latest snapshot for every camera
  GET  /api/v1/ai/detections/frame/{cam_id}  — annotated JPEG for one camera
"""
from __future__ import annotations

import base64
import time
from collections import deque
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import CageSocialSample, MouseStateSample, User
from ..security import get_current_user

router = APIRouter(prefix="/api/v1/ai", tags=["ai-detections"])

MAX_HISTORY = 120  # global ring-buffer, ~60 s at 2 fps per camera

# Per-camera state (keyed by cam_id string)
_latest_per_cam: dict[str, dict[str, Any]] = {}
_frames_per_cam: dict[str, bytes] = {}          # annotated JPEG per camera
_history: deque[dict[str, Any]] = deque(maxlen=MAX_HISTORY)

STALE_S = 12  # mark a camera stale if no push for this many seconds

# ── AI model registry (for the dashboard model-selector dropdown) ─────────────
# The inference service reports which classifiers it has loaded (via
# get_available_models()) on each push; the dashboard reads _available_models to
# populate the dropdown and POSTs the chosen id, which the inference loop reads
# back from the push response to switch its active model.
_DEFAULT_MODELS: list[dict[str, Any]] = [
    {"id": "state_v3", "name": "Behavioral State (RF v3)",
     "description": "Wake / Active, Quiet / Rest, Possible Sleep",
     "accuracy": "99.6%", "type": "per_mouse"},
    {"id": "social", "name": "Social Behavior",
     "description": "Huddling, Normal, Exploration",
     "accuracy": "100% (threshold-based)", "type": "group"},
]
_available_models: list[dict[str, Any]] = list(_DEFAULT_MODELS)
_active_model: str = "state_v3"

# ── Live AI inference settings ────────────────────────────────────────────────
# The inference loop reads these back from each push response and applies them
# on the next frame, so dashboard sliders take effect within ~1 frame. Bounds
# are validated when the user POSTs an update; defaults match run_inference.py.
_INFERENCE_SETTINGS_DEFAULTS: dict[str, float | int] = {
    "conf":             0.30,   # detection confidence threshold (0.05–0.95)
    "iou":              0.45,   # NMS IoU threshold (0.10–0.95)
    "imgsz":            640,    # input image size (320/480/640/960/1280)
    "frame_interval_s": 0.0,    # extra sleep between frames (0.0–5.0)
    "jpeg_quality":     70,     # annotated JPEG quality (30–95)
}
_inference_settings: dict[str, float | int] = dict(_INFERENCE_SETTINGS_DEFAULTS)
_SETTINGS_BOUNDS: dict[str, tuple[float, float]] = {
    "conf":             (0.05, 0.95),
    "iou":              (0.10, 0.95),
    "imgsz":            (320,  1280),
    "frame_interval_s": (0.0,  5.0),
    "jpeg_quality":     (30,   95),
}

# ── Behavioural-state persistence (for the camera-driven behaviour reports) ────
# Each push that carries per-mouse states is persisted (throttled to one sample
# per camera per settings.camera_sample_interval_s) so COUNT(*) * interval gives
# exact seconds-in-state. Best-effort: never breaks the live in-memory path.
_VALID_STATES = {"Wake / Active", "Quiet / Rest", "Possible Sleep"}
_VALID_BEHAVIORS = {"Normal", "Repetitive"}
_last_persist_ts: dict[str, float] = {}   # cam_id -> last persisted wall-clock


def _persist_samples(db: Session, payload: "DetectionPush", ts: float) -> None:
    settings = get_settings()
    if ts - _last_persist_ts.get(payload.cam_id, 0.0) < settings.camera_sample_interval_s:
        return
    cage_id = settings.camera_cage_map.get(payload.cam_id, settings.default_camera_cage)
    if not cage_id:
        return
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    rows: list[Any] = []
    for d in payload.detections:
        if not d.state:
            continue
        rows.append(MouseStateSample(
            cage_id=cage_id, cam_id=str(payload.cam_id)[:32],
            mouse_id=str(d.track_id or "?")[:8], ts=when,
            state=(d.state if d.state in _VALID_STATES else str(d.state)[:32]),
            behavior=(d.behavior if d.behavior in _VALID_BEHAVIORS else "Normal"),
            confidence=float(d.confidence or 0.0)))
    if payload.group:
        g = payload.group
        rows.append(CageSocialSample(
            cage_id=cage_id, cam_id=str(payload.cam_id)[:32], ts=when,
            social_state=str(g.get("social_state", "Unknown"))[:32],
            mean_interanimal_dist=float(g.get("mean_interanimal_dist", 0) or 0),
            active_count=int(g.get("active_count", 0) or 0),
            resting_count=int(g.get("resting_count", 0) or 0)))
    if rows:
        _last_persist_ts[payload.cam_id] = ts   # only advance throttle on a real write
        db.add_all(rows)
        db.commit()


# ── Pydantic models ───────────────────────────────────────────────────────────

class Detection(BaseModel):
    confidence: float = Field(..., ge=0.0, le=1.0)
    class_id: int = Field(default=0)
    class_name: str = Field(default="mouse")
    bbox: list[float] = Field(..., min_length=4, max_length=4)
    # Behavioural-monitoring extras (optional; from BehavioralMonitor)
    track_id: str | None = None
    state: str | None = None
    behavior: str | None = None
    centroid: list[float] | None = None


class DetectionPush(BaseModel):
    count: int
    detections: list[Detection] = []
    source: str = Field(default="colab_yolov8")
    model_version: str = Field(default="yolov8")
    cam_id: str = Field(default="0")        # required for multi-cam
    frame_width: int | None = None
    frame_height: int | None = None
    inference_ms: float | None = None
    frame_b64: str | None = None            # annotated JPEG as base64
    # Group social classification + the models the inference engine has loaded.
    group: dict[str, Any] | None = None
    available_models: list[dict[str, Any]] | None = None


class ActiveModelIn(BaseModel):
    active: str


class InferenceSettingsIn(BaseModel):
    """Partial update — only the keys you pass are changed."""
    conf:             float | None = None
    iou:              float | None = None
    imgsz:            int   | None = None
    frame_interval_s: float | None = None
    jpeg_quality:     int   | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/detections", status_code=201)
def push_detections(
    payload: DetectionPush,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Called by the inference loop for each camera frame."""
    global _available_models
    entry: dict[str, Any] = {
        "ts": time.time(),
        "count": payload.count,
        "detections": [d.model_dump() for d in payload.detections],
        "source": payload.source,
        "model_version": payload.model_version,
        "cam_id": payload.cam_id,
        "frame_width": payload.frame_width,
        "frame_height": payload.frame_height,
        "inference_ms": payload.inference_ms,
        "group": payload.group,
        "active_model": _active_model,
    }

    _latest_per_cam[payload.cam_id] = entry
    _history.append(entry)

    # Keep the model registry in sync with what the inference engine reports.
    if payload.available_models:
        _available_models = payload.available_models

    if payload.frame_b64:
        try:
            _frames_per_cam[payload.cam_id] = base64.b64decode(payload.frame_b64)
        except Exception:
            pass

    # Persist per-mouse states for the behaviour reports (throttled, best-effort —
    # must never break the live in-memory path above).
    try:
        _persist_samples(db, payload, entry["ts"])
    except Exception:
        db.rollback()

    # Return the active model + settings so the inference loop can apply them
    # on the next frame (live dashboard sliders take effect within ~1 frame).
    return {"status": "ok", "ts": entry["ts"], "count": payload.count,
            "cam_id": payload.cam_id, "active_model": _active_model,
            "settings": dict(_inference_settings)}


# ── Model selector (dashboard dropdown) ───────────────────────────────────────

@router.get("/models")
def get_models(_user: Annotated[User, Depends(get_current_user)]) -> dict[str, Any]:
    """Available AI models + the currently active one (for the dropdown)."""
    return {"available": _available_models, "active": _active_model}


@router.post("/models")
def set_active_model(
    payload: ActiveModelIn,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Set the active AI model. Validated against the reported available ids."""
    global _active_model
    valid_ids = {m.get("id") for m in _available_models}
    if valid_ids and payload.active not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_MODEL",
                    "message": f"Unknown model '{payload.active}'. Available: {sorted(valid_ids)}"},
        )
    _active_model = payload.active
    return {"active": _active_model, "available": _available_models}


@router.get("/settings")
def get_inference_settings(
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Current inference settings (read by the dashboard settings panel)."""
    return {"settings": dict(_inference_settings),
            "defaults": dict(_INFERENCE_SETTINGS_DEFAULTS),
            "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in _SETTINGS_BOUNDS.items()}}


@router.post("/settings")
def update_inference_settings(
    payload: InferenceSettingsIn,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Update one or more inference settings. Values are clamped to bounds; the
    inference loop reads them back via the push response and applies on the
    next frame."""
    updates = payload.model_dump(exclude_none=True)
    for key, val in updates.items():
        lo, hi = _SETTINGS_BOUNDS[key]
        clamped: float | int = max(lo, min(hi, float(val)))
        _inference_settings[key] = int(clamped) if isinstance(_INFERENCE_SETTINGS_DEFAULTS[key], int) else clamped
    return {"settings": dict(_inference_settings),
            "defaults": dict(_INFERENCE_SETTINGS_DEFAULTS),
            "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in _SETTINGS_BOUNDS.items()}}


@router.get("/detections/all")
def get_all(
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """
    Returns the latest detection snapshot for every camera that has pushed.
    Also includes an aggregate total_count and an overall live/stale flag.
    """
    now = time.time()
    cameras: dict[str, Any] = {}
    total_count = 0

    for cam_id, entry in _latest_per_cam.items():
        age = now - entry["ts"]
        cam_data = {
            **entry,
            "available": True,
            "stale": age > STALE_S,
            "age_s": round(age, 1),
            "has_frame": cam_id in _frames_per_cam,
        }
        cameras[cam_id] = cam_data
        if not cam_data["stale"]:
            total_count += entry["count"]

    any_live = any(not c["stale"] for c in cameras.values())

    return {
        "live": any_live,
        "total_count": total_count,
        "camera_count": len(cameras),
        "cameras": cameras,
    }


@router.get("/detections/frame/{cam_id}")
def get_frame(
    cam_id: str,
    _user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Return the latest annotated JPEG for the given camera."""
    frame = _frames_per_cam.get(cam_id)
    if frame is None:
        raise HTTPException(status_code=404, detail=f"No frame for camera {cam_id}")
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ── Backward-compat endpoints (single-camera clients) ─────────────────────────

@router.get("/detections/latest")
def get_latest(_user: Annotated[User, Depends(get_current_user)]) -> dict[str, Any]:
    """Returns the most recently pushed camera's detection (any cam)."""
    if not _latest_per_cam:
        return {"available": False, "stale": True, "count": 0,
                "detections": [], "source": None, "ts": None, "age_s": None}
    latest = max(_latest_per_cam.values(), key=lambda e: e["ts"])
    age = time.time() - latest["ts"]
    return {"available": True, "stale": age > STALE_S,
            "age_s": round(age, 1), **latest}


@router.get("/detections/frame")
def get_frame_any(_user: Annotated[User, Depends(get_current_user)]) -> Response:
    """Return any available annotated frame (most recent camera)."""
    if not _frames_per_cam or not _latest_per_cam:
        raise HTTPException(status_code=404, detail="No frame available yet")
    cam_id = max(_latest_per_cam.keys(), key=lambda k: _latest_per_cam[k]["ts"])
    return get_frame(cam_id, _user)


@router.get("/detections/history")
def get_history(
    _user: Annotated[User, Depends(get_current_user)],
    limit: int = 60,
) -> dict[str, Any]:
    limit = min(limit, MAX_HISTORY)
    items = list(_history)[-limit:]
    return {"count": len(items), "frames": list(reversed(items))}


@router.delete("/detections")
def clear_detections(_user: Annotated[User, Depends(get_current_user)]) -> dict[str, str]:
    _latest_per_cam.clear()
    _frames_per_cam.clear()
    _history.clear()
    return {"status": "cleared"}
