"""
Mouse-control proxy.

The camera-stream service (port 8090) owns the GPIO pins for the RC-toy
controller and exposes /mouse/* on the Pi's loopback only. To let the
dashboard at example.org (which only sees the FastAPI backend through
Cloudflare) drive the toy, we transparently proxy /api/v1/mouse/* to the
local camera-stream endpoints.

ONLY three endpoints are exposed:
  GET  /api/v1/mouse/status            — backend / pin map / active pins
  POST /api/v1/mouse/hold/{direction}  — hold the direction pin HIGH (refresh /400ms)
  POST /api/v1/mouse/stop              — force every pin LOW immediately

The old pulse-based `forward/back/left/right` and the random-walk endpoints
have been ripped out so nothing — no stale tab, no cached client — can
drive the toy except an actively-held button on the dashboard. Requests to
any removed path return 410 Gone from the camera service.

Everything sits behind the existing JWT / X-API-Key auth (via
`get_current_user`) so external callers can't poke the GPIO without
authenticating. The proxy adds a short timeout — if the camera service
is wedged we'd rather fail fast than hold a request open.
"""
from __future__ import annotations

import logging
import os
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ..models import User
from ..security import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mouse", tags=["mouse"])

CAMERA_URL = os.environ.get("CAMERA_STREAM_URL", "http://127.0.0.1:8090").rstrip("/")
TIMEOUT_S = float(os.environ.get("MOUSE_PROXY_TIMEOUT_S", "3.0"))

_DIRECTIONS = {"forward", "back", "left", "right"}


async def _proxy_get(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        try:
            r = await client.get(f"{CAMERA_URL}{path}")
        except (httpx.RequestError, httpx.TimeoutException) as e:
            log.warning({"event": "mouse.proxy.unreachable", "path": path, "error": str(e)})
            raise HTTPException(
                status_code=502,
                detail={"code": "CAMERA_SERVICE_DOWN",
                        "message": "Camera-stream service is unreachable."},
            ) from e
    if r.status_code >= 500:
        raise HTTPException(status_code=502, detail={"code": "CAMERA_SERVICE_ERROR",
                                                     "message": r.text[:200]})
    try:
        return r.json()
    except ValueError:
        raise HTTPException(status_code=502, detail={"code": "CAMERA_SERVICE_BAD_JSON",
                                                     "message": r.text[:200]})


async def _proxy_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        try:
            r = await client.post(f"{CAMERA_URL}{path}", json=body)
        except (httpx.RequestError, httpx.TimeoutException) as e:
            log.warning({"event": "mouse.proxy.unreachable", "path": path, "error": str(e)})
            raise HTTPException(
                status_code=502,
                detail={"code": "CAMERA_SERVICE_DOWN",
                        "message": "Camera-stream service is unreachable."},
            ) from e
    if r.status_code == 404:
        raise HTTPException(status_code=404,
                            detail={"code": "NOT_FOUND", "message": "Unknown action."})
    if r.status_code >= 500:
        raise HTTPException(status_code=502, detail={"code": "CAMERA_SERVICE_ERROR",
                                                     "message": r.text[:200]})
    try:
        return r.json()
    except ValueError:
        raise HTTPException(status_code=502, detail={"code": "CAMERA_SERVICE_BAD_JSON",
                                                     "message": r.text[:200]})


@router.get("/status")
async def status(_user: Annotated[User, Depends(get_current_user)]) -> dict[str, Any]:
    return await _proxy_get("/mouse/status")


@router.post("/hold/{direction}")
async def hold(
    direction: str,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Keep a direction GPIO HIGH. Call on pointer-down and every ~400 ms
    while the button is held. Auto-releases 1.5 s after the last call
    (watchdog), so the GPIO returns LOW even if the browser drops."""
    if direction not in _DIRECTIONS:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": f"Unknown direction: {direction}"},
        )
    return await _proxy_post(f"/mouse/hold/{direction}", {})


@router.post("/stop")
async def stop(_user: Annotated[User, Depends(get_current_user)]) -> dict[str, Any]:
    """Force every GPIO pin LOW immediately. Called on every button release
    (pointer-up / leave / cancel) so the toy stops the instant the user
    lets go."""
    return await _proxy_post("/mouse/stop", {})
