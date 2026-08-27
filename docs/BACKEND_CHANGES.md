# Backend Changes, Detailed Changelog

> All changes listed below were introduced during the current development sprint.
> Commits are ordered from oldest → newest.

---

## 1.`backend/app/main.py`, Critical SPA / routing fixes

### Problem
All`GET` routes on`example.org` were hanging (timing out). Only`POST` routes
and`/docs` worked. Root cause: **`BaseHTTPMiddleware` +`FileResponse`
deadlock**, a known Starlette bug where the middleware's streaming buffers
stall under Cloudflare tunnel buffering, exhausting the worker pool so no other
route could respond.

### Changes

| What | Detail |
|---|---|
| **Cache`index.html` at boot** |`_INDEX_BYTES = _INDEX_PATH.read_bytes()`, reads once at startup into memory |
| **Replace`FileResponse` with`Response`** | SPA fallback now returns`Response(content=_INDEX_BYTES, media_type="text/html")`, no per-request file I/O, no middleware deadlock |
| **API/WS fast-exit guard** | Paths starting with`api/`,`ws/`, or equal to`health`/`openapi.json` return`404` immediately instead of falling through to the SPA handler |
| **Path traversal guard** |`if ".." in full_path: return Response(status_code=404)` |
| **Register`detections` router** | Added`from .api import detections` and`app.include_router(detections.router)` |

### Before vs After

```python
# BEFORE — caused hangs
@app.get("/{full_path:path}", include_in_schema=False)
async def _spa_fallback(full_path: str):
    return FileResponse(str(_STATIC / "index.html"))   # ← blocks under CF tunnel

# AFTER — in-memory, no stall
_INDEX_BYTES = _INDEX_PATH.read_bytes() if _INDEX_PATH.is_file() else b""

@app.get("/{full_path:path}", include_in_schema=False)
async def _spa_fallback(full_path: str):
    if ".." in full_path:
        return Response(status_code=404)
    if full_path.startswith(("api/", "ws/")) or full_path in ("health", "openapi.json"):
        return Response(status_code=404)
    if not _INDEX_BYTES:
        return Response(status_code=503, content=b"Frontend not built yet")
    return Response(content=_INDEX_BYTES, media_type="text/html")
```

---

## 2.`backend/app/api/detections.py`, New file (multi-camera AI detection)

**Commit:**`feat: multi-camera live detection`

Entirely new module. Replaces the old single-camera detection stub with a
full multi-camera state store and REST API.

### Architecture

```
Colab / pi-inference  ──POST /api/v1/ai/detections──►  _latest_per_cam[cam_id]
                                                         _frames_per_cam[cam_id]
                                                         _history (ring-buffer 120)

Dashboard  ──GET /api/v1/ai/detections/all──────────►  per-camera snapshots + stale flags
           ──GET /api/v1/ai/detections/frame/{cam_id}► annotated JPEG bytes
```

### In-memory state

```python
_latest_per_cam: dict[str, dict]   # latest detection entry per camera
_frames_per_cam: dict[str, bytes]  # latest annotated JPEG per camera
_history: deque(maxlen=120)        # global ring-buffer (~60 s at 2 fps)
STALE_S = 12                       # camera marked stale if no push for 12 s
```

### Pydantic models

| Model | Fields |
|---|---|
|`Detection` |`confidence`,`class_id`,`class_name`,`bbox[4]` |
|`DetectionPush` |`count`,`detections[]`,`source`,`model_version`,`cam_id`,`frame_width`,`frame_height`,`inference_ms`,`frame_b64` (base64 JPEG) |

### Routes

| Method | Path | Auth | Description |
|---|---|---|---|
|`POST` |`/api/v1/ai/detections` | ✅ JWT | Push one camera's frame + detections |
|`GET` |`/api/v1/ai/detections/all` | ✅ JWT | Latest snapshot for all cameras, stale flags, total count |
|`GET` |`/api/v1/ai/detections/frame/{cam_id}` | ✅ JWT | Annotated JPEG for a specific camera |
|`GET` |`/api/v1/ai/detections/latest` | ✅ JWT | Most-recent camera (backward compat) |
|`GET` |`/api/v1/ai/detections/frame` | ✅ JWT | Most-recent camera frame (backward compat) |
|`GET` |`/api/v1/ai/detections/history` | ✅ JWT | Last N entries from ring-buffer |
|`DELETE` |`/api/v1/ai/detections` | ✅ JWT | Clear all state |

### Key details

-`frame_b64` is decoded with`base64.b64decode()` and stored as raw bytes; served back with`media_type="image/jpeg"` and`Cache-Control: no-cache`
-`GET /detections/all` computes`age_s` and`stale` flag per camera on every request (no caching)
-`has_frame: bool` field tells the dashboard whether an annotated image is available before attempting the frame fetch

---

## 3.`backend/app/api/system.py`, Fan level extended + Pi proxy routes

### Fan validation: support for Auto mode (`level = -1`)

**Commit:**`fix: fan Off/Low/Med/High now stick; add Auto mode`

```python
# BEFORE
if not isinstance(level, int) or not (0 <= level <= 4):
    raise HTTPException(400, "level must be 0–4")

# AFTER
if not isinstance(level, int) or not (-1 <= level <= 4):
    raise HTTPException(400, "level must be an integer -1 (auto) or 0–4")
```

**Meaning of values:**

| Level | Meaning |
|---|---|
|`-1` | Auto, Pi thermal daemon controls the fan |
|`0` | Off |
|`1` | Low |
|`2` | Medium |
|`3` | High |
|`4` | Max (delegates to`fan-max.service`) |

### Pi proxy routes (added for System page dashboard)

All routes proxy through to the camera-stream service running at`:8090` on
the Pi via`httpx` async client (timeout 10 s).

| Method | Path | Proxies to | Description |
|---|---|---|---|
|`GET` |`/api/v1/system/pi` |`GET /system/status` | Live CPU temp, %, memory, fan level, uptime |
|`POST` |`/api/v1/system/pi/fan` |`POST /system/fan` | Set fan level (-1 to 4) |
|`POST` |`/api/v1/system/pi/reboot` |`POST /system/reboot` | Reboot the Pi |
|`POST` |`/api/v1/system/pi/shutdown` |`POST /system/shutdown` | Shut down the Pi |

Helper functions`_cam_get()` and`_cam_post()` handle connection errors,
5xx responses, and JSON parse failures, all converted to`502 Bad Gateway`
with structured`{"code": "PI_UNREACHABLE", "message": "..."}` bodies.

---

## 4. Summary of all commits (backend-touching)

| Commit | Hash | Change |
|---|---|---|
| Move Pi hardware control to System page |`47c4ab9` | Removed`SystemControl` from Schedule page; added`PiHardwareControl` to System page (frontend only, backend routes were already there) |
| Colab YOLOv8 live detection |`cb351a6` | Initial single-camera detection push endpoint |
| Multi-camera live detection |`1f3a903` | Rewrote`detections.py`, per-`cam_id` state,`/all` endpoint |
| Fix illegal`setState` in`useQuery` select |`f27dea7` | Frontend only, no backend change |
| Fix JWT auth for annotated frames |`50621c4` | Frontend`useAuthFrame` hook, no backend change |
| Fan Off / Auto mode fix |`0dfc5c1` |`system.py` fan validation`-1 ≤ level ≤ 4`;`server.py` keep-alive thread |
| Camera 30 fps + long-poll |`a4aeead` /`390043d` |`server.py` only, no backend change |
| **Fix hung GETs (index.html cache)** |`be99971` |`main.py`, SPA fallback reads`_INDEX_BYTES` at boot, returns`Response` not`FileResponse` |
| Pi-inference NCNN |`812abc1` |`device/pi-inference/` only, no backend change |
| Persist fan target across restarts |`a7251d2` |`server.py``/var/tmp/ivc-fan-target` persistence, no backend change |

---

## 5. API surface, quick reference

```
/api/v1/ai/
  POST   /detections                    Push camera frame + detections
  GET    /detections/all                All cameras snapshot
  GET    /detections/frame/{cam_id}     Annotated JPEG (JWT required)
  GET    /detections/frame              Most recent camera JPEG (compat)
  GET    /detections/latest             Most recent detection entry (compat)
  GET    /detections/history?limit=60   Ring-buffer history
  DELETE /detections                    Clear all state

/api/v1/system/
  GET    /status                        Backend health, versions, services
  GET    /logs?lines=80                 Tail deploy log
  GET    /deploys?limit=10              Recent deploy SHA list
  GET    /anomaly                       Anomaly detector baselines
  GET    /pi                            Live Pi hardware stats (proxied)
  POST   /pi/fan                        Set fan level -1..4 (proxied)
  POST   /pi/reboot                     Reboot Pi (proxied)
  POST   /pi/shutdown                   Shutdown Pi (proxied)
```

---

*Generated: 2026-05-29, IVC Cage project sprint*
