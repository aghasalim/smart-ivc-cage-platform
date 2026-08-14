"""
DHT11 → Backend bridge.

Polls the camera-stream service at /sensor/env (which caches the Arduino's
DHT11 reading) and POSTs it to the backend's /api/v1/ingest endpoint every
INTERVAL_S seconds so it shows up on charts + survives restarts.

Auths once at boot, refreshes the JWT if it expires (401).

Env vars:
  ING_BACKEND_URL   default http://127.0.0.1:8000
  ING_CAMERA_URL    default http://127.0.0.1:8090
  ING_USERNAME      required
  ING_PASSWORD      required
  ING_CAGE_ID       default cage-001
  ING_INTERVAL_S    default 30
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("env-ingester")

BACKEND  = os.environ.get("ING_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
CAMERA   = os.environ.get("ING_CAMERA_URL",  "http://127.0.0.1:8090").rstrip("/")
USERNAME = os.environ.get("ING_USERNAME")
PASSWORD = os.environ.get("ING_PASSWORD")
CAGE_ID  = os.environ.get("ING_CAGE_ID", "cage-001")
INTERVAL = float(os.environ.get("ING_INTERVAL_S", "30"))
# Don't post readings older than this (DHT11 hasn't responded in a while)
MAX_AGE  = float(os.environ.get("ING_MAX_AGE_S", "60"))

if not USERNAME or not PASSWORD:
    log.error("ING_USERNAME and ING_PASSWORD must be set")
    sys.exit(2)


def login() -> str:
    """Exchange username/password for a JWT. Retries forever — backend may not
    be up yet on first boot."""
    delay = 2
    while True:
        try:
            r = httpx.post(
                f"{BACKEND}/api/v1/auth/login",
                json={"username": USERNAME, "password": PASSWORD},
                timeout=10,
            )
            r.raise_for_status()
            token = r.json()["access_token"]
            log.info("logged in as %s", USERNAME)
            return token
        except Exception as e:
            log.warning("login failed (%s) — retrying in %ds", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def fetch_env() -> Optional[dict]:
    """Get the latest DHT11 reading from the camera service."""
    try:
        r = httpx.get(f"{CAMERA}/sensor/env", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("camera /sensor/env failed: %s", e)
        return None


def post_reading(token: str, temp_c: float, hum_pct: float, seq: int) -> tuple[bool, bool]:
    """POST a single environment reading. Returns (ok, needs_relogin)."""
    body = {
        "cage_id": CAGE_ID,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "seq": seq,
        "sensors": {
            "environment": {
                "temperature_c": round(temp_c, 2),
                "humidity_pct":  round(hum_pct, 2),
            }
        },
    }
    try:
        r = httpx.post(
            f"{BACKEND}/api/v1/ingest",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 401:
            return False, True
        r.raise_for_status()
        return True, False
    except httpx.HTTPStatusError as e:
        log.warning("ingest http %s: %s", e.response.status_code, e.response.text[:200])
        return False, False
    except Exception as e:
        log.warning("ingest error: %s", e)
        return False, False


def main() -> None:
    log.info(
        "starting: backend=%s camera=%s cage=%s interval=%.0fs",
        BACKEND, CAMERA, CAGE_ID, INTERVAL,
    )
    token = login()
    seq = int(time.time())  # monotonic-ish across restarts

    while True:
        env = fetch_env()
        if env and env.get("connected") and env.get("temperature_c") is not None:
            age = env.get("age_s") or 0
            if age > MAX_AGE:
                log.warning("skipping stale reading (age=%ss)", age)
            else:
                ok, relogin = post_reading(
                    token,
                    env["temperature_c"],
                    env["humidity_pct"],
                    seq,
                )
                if relogin:
                    log.info("token expired — re-logging in")
                    token = login()
                elif ok:
                    log.info(
                        "ingested temp=%.1f°C hum=%.1f%% (age=%ss seq=%d)",
                        env["temperature_c"], env["humidity_pct"], age, seq,
                    )
                seq += 1
        else:
            log.warning("no fresh DHT11 reading available — sleeping")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
# remote-deploy test: 2026-05-20T15:44:24+02:00
