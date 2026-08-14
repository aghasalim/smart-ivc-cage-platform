"""Feeding-window scheduler.

Periodically evaluates every active ``ScheduleWindow`` and, on the transition
from outside-window to inside-window (or vice versa), drives the food gate
servo via the Arduino-bridge command queue.

This is what makes "Active + Mon..Sun + 14:39–14:40" on the dashboard actually
open the gate at 14:39 and close it at 14:40 — without this loop, the rows are
just stored in the DB and nothing happens.

The Arduino accepts ``{"servo":"food","angle":N}`` (also ``"feed"``). We write
the JSON line to the bridge's command file ``/tmp/ivc-arduino-cmd``; the
``ivc-arduino-bridge`` service reads it and forwards it over USB.

State (last commanded open/closed per cage) is held in process memory — on
restart, the very first tick re-asserts the correct state, which is benign
because the firmware ignores no-op moves (and our servo helpers no-op when the
target angle equals the last commanded angle by design).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import ScheduleWindow

log = logging.getLogger(__name__)
settings = get_settings()

# Bridge command queue (same file the camera-stream /pump and /servo handlers
# use). Overridable for tests.
CMD_FILE: str = os.environ.get("IVC_ARDUINO_CMD_FILE", "/tmp/ivc-arduino-cmd")

# How often to evaluate windows. 15 s gives sub-minute responsiveness without
# flooding the bridge — the dashboard rounds windows to whole minutes anyway.
TICK_SECONDS: float = 15.0

# Servo angles. Match the dashboard defaults (Schedule.tsx CLOSE_ANGLE=0,
# OPEN_ANGLE=90).
OPEN_ANGLE: int = 90
CLOSE_ANGLE: int = 0


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":", 1)
    return int(h), int(m)


def _is_inside_window(window: ScheduleWindow, now_utc: datetime) -> bool:
    """True if ``now_utc`` falls inside this window in the window's own
    timezone, today is one of its days, and the window is active."""
    if not window.active:
        return False
    try:
        tz = ZoneInfo(window.timezone or "Europe/Brussels")
    except Exception:
        tz = ZoneInfo("Europe/Brussels")
    now_local = now_utc.astimezone(tz)
    try:
        days = {int(d) for d in (window.days_of_week or "").split(",") if d.strip() != ""}
    except ValueError:
        days = set()
    # Python weekday(): Mon=0..Sun=6 — matches the dashboard's day-button index.
    if now_local.weekday() not in days:
        return False
    try:
        sh, sm = _parse_hhmm(window.start_local)
        eh, em = _parse_hhmm(window.end_local)
    except (ValueError, AttributeError):
        return False
    start = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now_local.replace(hour=eh, minute=em, second=0, microsecond=0)
    # Same-day window (we don't support windows that cross midnight on the UI,
    # so end <= start means an invalid/empty window and we treat it as closed).
    if end <= start:
        return False
    return start <= now_local < end


def _any_window_open(windows: list[ScheduleWindow], now_utc: datetime) -> bool:
    """Cage is "open" if ANY of its active windows is currently open. The UI
    today only edits one window, but the data model is a list — handle both."""
    return any(_is_inside_window(w, now_utc) for w in windows)


def _send_food_gate(angle: int) -> bool:
    """Atomically write a food-gate command to the bridge queue. Returns True
    on success, False on I/O error (the bridge will retry on the next tick)."""
    cmd = json.dumps({"servo": "food", "angle": int(angle)})
    try:
        # Atomic write: tmp file in the same dir + rename.
        path = Path(CMD_FILE)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(cmd + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as exc:
        log.warning({"event": "scheduler.cmd_write_failed", "err": str(exc)[:120]})
        return False


def _load_windows_by_cage(db: Session) -> dict[str, list[ScheduleWindow]]:
    rows = db.query(ScheduleWindow).filter(ScheduleWindow.active == True).all()  # noqa: E712
    by_cage: dict[str, list[ScheduleWindow]] = {}
    for w in rows:
        by_cage.setdefault(w.cage_id, []).append(w)
    return by_cage


async def scheduler_loop() -> None:
    """Async loop — started from the FastAPI lifespan. Cancellation-safe."""
    log.info({"event": "scheduler.start", "tick_s": TICK_SECONDS, "cmd_file": CMD_FILE})
    # Last known cage state — None = unknown (first iteration always asserts).
    last_open: dict[str, bool] = {}
    while True:
        try:
            db = SessionLocal()
            try:
                by_cage = _load_windows_by_cage(db)
            finally:
                db.close()

            now = datetime.now(tz=ZoneInfo("UTC"))
            for cage_id, windows in by_cage.items():
                is_open = _any_window_open(windows, now)
                prev = last_open.get(cage_id)
                if prev is None or prev != is_open:
                    ok = _send_food_gate(OPEN_ANGLE if is_open else CLOSE_ANGLE)
                    log.info({
                        "event": "scheduler.transition",
                        "cage": cage_id,
                        "is_open": is_open,
                        "sent": ok,
                    })
                    if ok:
                        last_open[cage_id] = is_open
                else:
                    # No transition — nothing to send. The firmware already
                    # auto-detaches the servo after settling, so no PWM is
                    # being driven while idle.
                    pass

            # Cages that no longer have active windows: forget their state so a
            # future re-enable triggers the next transition cleanly.
            stale = [c for c in last_open if c not in by_cage]
            for c in stale:
                last_open.pop(c, None)

        except asyncio.CancelledError:
            log.info({"event": "scheduler.cancelled"})
            raise
        except Exception as exc:
            log.warning({"event": "scheduler.tick_error", "err": str(exc)[:200]})

        try:
            await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            log.info({"event": "scheduler.cancelled"})
            raise
