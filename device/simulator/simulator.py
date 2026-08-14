"""IVC cage simulator.

Logs in once, then publishes a sensor packet for each configured cage every
``SIM_INTERVAL_SECONDS``.  Simulated local time advances ``SIM_TIME_SCALE``
times faster than wall clock so the dashboard shows realistic 24-hour
behaviour without waiting 24 hours.

Also exposes a small FastAPI status page on port 8001.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI

from profiles import PROFILES, get_profile
from sensors import (
    behaviour_state,
    feeding_state,
    metabolic_state,
    water_state,
    weighing_state,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("simulator")

BACKEND_URL = os.environ.get("SIM_BACKEND_URL", "http://backend:8000").rstrip("/")
CAGE_IDS = [c.strip() for c in os.environ.get("SIM_CAGE_IDS", "cage-001,cage-002,cage-003").split(",") if c.strip()]
INTERVAL = float(os.environ.get("SIM_INTERVAL_SECONDS", "5"))
USERNAME = os.environ.get("SIM_USERNAME", "researcher")
PASSWORD = os.environ.get("SIM_PASSWORD", "change-me-please")
TIME_SCALE = float(os.environ.get("SIM_TIME_SCALE", "60"))


class State:
    token: str | None = None
    # Start seq from current epoch so restarts never collide with previous runs
    _seq_epoch: int = int(time.time())
    seq: dict[str, int] = {}
    feeding: dict[str, dict] = {c: {} for c in CAGE_IDS}
    water: dict[str, dict] = {c: {} for c in CAGE_IDS}
    started_wall = time.time()
    started_local_hour = 22.0
    total_sent = 0
    total_failed = 0
    last_publish: dict[str, datetime] = {}

    @classmethod
    def local_hour(cls) -> float:
        elapsed_wall = time.time() - cls.started_wall
        elapsed_simulated = elapsed_wall * TIME_SCALE
        h = (cls.started_local_hour + elapsed_simulated / 3600.0) % 24.0
        return h


async def login(client: httpx.AsyncClient) -> str:
    log.info("logging in as %s", USERNAME)
    for attempt in range(30):
        try:
            r = await client.post(
                f"{BACKEND_URL}/api/v1/auth/login",
                json={"username": USERNAME, "password": PASSWORD},
                timeout=10.0,
            )
            if r.status_code == 200:
                return r.json()["access_token"]
            log.warning("login failed (%s): %s", r.status_code, r.text[:200])
        except httpx.HTTPError as exc:
            log.warning("login error: %s (attempt %s)", exc, attempt + 1)
        await asyncio.sleep(2)
    raise RuntimeError("could not log in to backend")


async def ensure_cages(client: httpx.AsyncClient) -> None:
    """Create the simulator's cages on the backend if they don't exist yet."""
    r = await client.get(
        f"{BACKEND_URL}/api/v1/cages",
        headers={"Authorization": f"Bearer {State.token}"},
        timeout=10.0,
    )
    if r.status_code != 200:
        log.warning("cannot list cages: %s", r.text[:200])
        return
    existing = {c["id"] for c in r.json()}
    for cage_id in CAGE_IDS:
        if cage_id in existing:
            continue
        log.info("creating missing cage %s on backend", cage_id)
        # researcher cannot create cages (admin-only), but the seed ensures
        # the three default cages already exist. Skip silently if forbidden.
        try:
            await client.post(
                f"{BACKEND_URL}/api/v1/cages",
                json={"id": cage_id, "name": cage_id},
                headers={"Authorization": f"Bearer {State.token}"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            log.warning("could not create %s: %s", cage_id, exc)


async def publish_once(client: httpx.AsyncClient, cage_id: str) -> None:
    hour = State.local_hour()
    profile = get_profile(cage_id)
    State.seq[cage_id] = State.seq.get(cage_id, State._seq_epoch) + 1
    payload = {
        "cage_id": cage_id,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "seq": State.seq[cage_id],
        "sensors": {
            "feeding": feeding_state(hour, profile, State.feeding[cage_id]),
            "water": water_state(hour, profile, State.water[cage_id]),
            "metabolic": metabolic_state(hour, profile),
            "behaviour": behaviour_state(hour, profile),
            "weighing": weighing_state(hour, profile),
        },
    }
    try:
        r = await client.post(
            f"{BACKEND_URL}/api/v1/ingest",
            json=payload,
            headers={"Authorization": f"Bearer {State.token}"},
            timeout=10.0,
        )
        if r.status_code == 202:
            State.total_sent += 1
            State.last_publish[cage_id] = datetime.now(tz=timezone.utc)
        elif r.status_code == 401:
            log.info("token expired — refreshing")
            State.token = await login(client)
        else:
            State.total_failed += 1
            log.warning("ingest non-202 (%s): %s", r.status_code, r.text[:200])
    except httpx.HTTPError as exc:
        State.total_failed += 1
        log.warning("ingest error: %s", exc)


async def publisher_loop() -> None:
    async with httpx.AsyncClient() as client:
        State.token = await login(client)
        await ensure_cages(client)
        log.info(
            "publishing every %.1fs for cages=%s (time scale %.1fx)",
            INTERVAL, ",".join(CAGE_IDS), TIME_SCALE,
        )
        while True:
            for cage_id in CAGE_IDS:
                await publish_once(client, cage_id)
            await asyncio.sleep(INTERVAL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(publisher_loop())
    yield
    task.cancel()


app = FastAPI(title="IVC Simulator", lifespan=lifespan)


@app.get("/status")
def status():
    return {
        "backend_url": BACKEND_URL,
        "cages": CAGE_IDS,
        "interval_seconds": INTERVAL,
        "time_scale": TIME_SCALE,
        "simulated_local_hour": round(State.local_hour(), 3),
        "total_sent": State.total_sent,
        "total_failed": State.total_failed,
        "logged_in": State.token is not None,
        "last_publish_per_cage": {k: v.isoformat() for k, v in State.last_publish.items()},
    }


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
