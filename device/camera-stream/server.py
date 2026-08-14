"""
IVC Cage – multi-camera MJPEG streaming + Arduino sensor bridge service.
GPIO pins: Up=17, Down=27, Left=22, Right=23 (single pin per direction).
Deploy script now kills stale python3 on port 8090 before restart.

Each camera runs a persistent ffmpeg process in a background thread that keeps
a "latest frame" cached. Snapshot/stream endpoints serve from the cache, so
N concurrent clients never contend for the V4L2 device.
"""

from __future__ import annotations

import atexit
import glob
import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, asdict
from typing import Callable, Dict, Iterator, List, Optional

from flask import Flask, Response, jsonify, abort, render_template_string, request


# --- Camera discovery ------------------------------------------------------

# Camera capture parameters — overridable via env so we can tune without redeploy.
# 940nm IR USB cameras (driver-free, 1080P) output MJPEG on-device, so 1080p@30fps
# is cheap CPU-wise on the Pi (no re-encode needed — just passthrough the JPEG).
_CAM_WIDTH  = int(os.environ.get("CAMERA_WIDTH",  "1920"))
_CAM_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", "1080"))
_CAM_FPS    = int(os.environ.get("CAMERA_FPS",    "30"))


@dataclass
class Camera:
    id: str
    name: str
    device: str
    bus: str
    width: int    = _CAM_WIDTH
    height: int   = _CAM_HEIGHT
    fps: int      = _CAM_FPS
    supports_mjpeg: bool = True   # False → YUYV fallback with Pi-side re-encode


def _v4l2_list() -> List[Camera]:
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", "--list-devices"], stderr=subprocess.DEVNULL
        ).decode()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    cameras: List[Camera] = []
    cur_name: Optional[str] = None
    cur_bus: Optional[str] = None
    for raw in out.splitlines():
        line = raw.rstrip()
        if not line:
            cur_name = None
            cur_bus = None
            continue
        if not line.startswith("\t"):
            m = re.match(r"^(?P<name>.+?)\s*\((?P<bus>[^)]+)\)\s*:?$", line)
            if m:
                cur_name = m.group("name").strip()
                cur_bus = m.group("bus").strip()
            else:
                cur_name = line.rstrip(":").strip()
                cur_bus = ""
            continue
        dev = line.strip()
        if not dev.startswith("/dev/video"):
            continue
        if cur_name is None or cur_bus is None:
            continue
        name_lc = (cur_name or "").lower()
        bus_lc = cur_bus.lower()
        if any(s in name_lc or s in bus_lc for s in ("pispbe", "pisp_be", "rpi-hevc", "platform:")):
            continue
        if any(c.bus == cur_bus for c in cameras):
            continue
        mjpeg = _probe_mjpeg(dev)
        if mjpeg:
            _v4l2_init_ir(dev)  # set IR-optimal V4L2 controls before capture starts
        cameras.append(Camera(id=str(len(cameras)), name=cur_name, device=dev,
                              bus=cur_bus, supports_mjpeg=mjpeg))
    return cameras


def _probe_mjpeg(device: str) -> bool:
    """Return True if the camera advertises MJPEG as a capture format.

    Driver-free 940nm USB cameras always have MJPEG — the sensor does the
    compression on-chip, so we can passthrough without re-encoding on the Pi.
    Cameras that only offer YUYV/YUV420 fall back to Pi-side JPEG re-encode.
    """
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", f"--device={device}", "--list-formats-ext"],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode()
        return "MJPG" in out or "MJPEG" in out
    except Exception:
        return True  # assume MJPEG if v4l2-ctl not available; ffmpeg will error-out if wrong


def _v4l2_init_ir(device: str) -> None:
    """Apply V4L2 controls optimised for 940nm IR night-vision cameras.

    Problems without these:
      - Auto white-balance (AWB) fights the monochrome IR scene, producing
        green/magenta colour casts and a pulsing brightness.
      - Auto-exposure hunts continuously because the scene looks uniformly
        grey to the sensor — results in massive brightness swings every few
        seconds under constant IR illumination.
      - Auto-focus (if present) chases phantom edges in the IR image.

    We set manual exposure at ~6 ms which works well with the 940nm LED board
    at typical cage distances. Tune CAMERA_EXPOSURE env var if needed.
    """
    # Exposure in 100µs steps. Default 60 (=6 ms) is good for a normally-lit
    # room; raise CAMERA_EXPOSURE for a dark IR-only cage, lower it if washed out.
    # NOTE: the modern UVC control is `exposure_time_absolute` (older alias
    # `exposure_absolute`); we set BOTH names so it applies on every driver —
    # the previous code only set the old alias, so manual exposure silently
    # failed and the camera stayed at its bright default → washed-out white.
    exposure_abs = int(os.environ.get("CAMERA_EXPOSURE", "60"))   # 100µs steps
    gain_val     = int(os.environ.get("CAMERA_GAIN",     "16"))   # 1..128
    controls = [
        # Disable auto-white-balance — IR images have no colour cast to correct
        ("white_balance_temperature_auto", "0"),
        ("white_balance_automatic",        "0"),  # alias on some UVC drivers
        # Switch to manual exposure (V4L2 menu: 1 = Manual, 3 = Aperture/Auto)
        ("exposure_auto",                  "1"),
        ("auto_exposure",                  "1"),  # alias / modern name
        ("exposure_absolute",              str(exposure_abs)),       # old alias
        ("exposure_time_absolute",         str(exposure_abs)),       # modern name (the one that works here)
        # Disable auto-gain — fixed gain avoids noise pumping
        ("gain_automatic",                 "0"),
        ("gain",                           str(gain_val)),
        # Disable autofocus if the module has one
        ("focus_auto",                     "0"),
        ("focus_automatic_continuous",     "0"),  # modern name
        # 50 Hz power-line frequency (EU labs) — avoids flicker from fluorescent
        ("power_line_frequency",           "1"),
    ]
    for ctrl, val in controls:
        subprocess.run(
            ["v4l2-ctl", f"--device={device}", f"--set-ctrl={ctrl}={val}"],
            capture_output=True, timeout=2,
        )
    print(f"[cam] IR V4L2 controls applied to {device} "
          f"(AWB off, manual exposure={exposure_abs}, gain={gain_val})", flush=True)


# --- Persistent capture thread per camera ----------------------------------

class CameraCapture:
    """Keeps one ffmpeg running per camera. Caches the latest JPEG frame."""

    def __init__(self, cam: Camera) -> None:
        self.cam = cam
        self._latest: Optional[bytes] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._frame_seq = 0
        self._thread = threading.Thread(target=self._loop, name=f"cap-{cam.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_latest(self) -> Optional[bytes]:
        with self._lock:
            return self._latest

    def wait_next(self, timeout: float = 1.0) -> Optional[bytes]:
        """Block until a new frame arrives (or timeout)."""
        with self._cond:
            current = self._frame_seq
            self._cond.wait_for(lambda: self._frame_seq != current or self._stop.is_set(),
                                timeout=timeout)
        return self.get_latest()

    def wait_after(self, since_seq: int, timeout: float = 10.0) -> tuple[Optional[bytes], int]:
        """Block until frame_seq > since_seq, then return (frame, current_seq).
        Used by the long-polling snapshot endpoint so the client can re-fetch
        immediately when a new frame arrives — no artificial wait between polls.
        """
        deadline = time.time() + timeout
        with self._cond:
            while self._frame_seq <= since_seq and not self._stop.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            seq = self._frame_seq
        return self.get_latest(), seq

    def _loop(self) -> None:
        # Outer loop: respawn ffmpeg if it dies (camera unplug / re-plug, USB hiccup)
        while not self._stop.is_set():
            if self.cam.supports_mjpeg:
                # ── MJPEG passthrough (940nm IR cameras, driver-free) ──────────
                # The camera compresses frames on-chip.  We ask for MJPEG input
                # and copy the stream straight to stdout — zero Pi CPU decode/re-encode.
                # -rtbufsize raised to 100M so a momentary USB burst at 1080p
                # doesn't force ffmpeg to drop frames.
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-fflags", "nobuffer",
                    "-flags", "low_delay",
                    "-f", "v4l2",
                    "-input_format", "mjpeg",
                    "-video_size", f"{self.cam.width}x{self.cam.height}",
                    "-framerate", str(self.cam.fps),
                    "-thread_queue_size", "8",
                    "-rtbufsize", "100M",
                    "-i", self.cam.device,
                    "-c:v", "copy",   # passthrough — no CPU re-encode
                    "-f", "mjpeg",
                    "pipe:1",
                ]
            else:
                # ── YUYV fallback ────────────────────────────────────────────
                # Camera only supports raw YUV — re-encode to JPEG on the Pi.
                # Quality 4 ≈ 85% JPEG at manageable bitrate; drop -q:v or
                # set CAMERA_JPEG_QUALITY=2 for higher fidelity.
                q = int(os.environ.get("CAMERA_JPEG_QUALITY", "4"))
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-fflags", "nobuffer",
                    "-flags", "low_delay",
                    "-f", "v4l2",
                    "-video_size", f"{self.cam.width}x{self.cam.height}",
                    "-framerate", str(self.cam.fps),
                    "-thread_queue_size", "8",
                    "-i", self.cam.device,
                    "-c:v", "mjpeg",
                    "-q:v", str(q),   # 2=best … 31=worst
                    "-f", "mjpeg",
                    "pipe:1",
                ]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
            except Exception as e:
                print(f"[cap-{self.cam.id}] ffmpeg spawn failed: {e}", flush=True)
                time.sleep(2)
                continue
            assert proc.stdout is not None
            print(f"[cap-{self.cam.id}] ffmpeg started for {self.cam.device}", flush=True)
            buf = b""
            try:
                while not self._stop.is_set():
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    # Extract complete JPEG frames (SOI=FFD8, EOI=FFD9)
                    while True:
                        soi = buf.find(b"\xff\xd8")
                        if soi < 0:
                            buf = b""
                            break
                        eoi = buf.find(b"\xff\xd9", soi + 2)
                        if eoi < 0:
                            if soi > 0:
                                buf = buf[soi:]
                            break
                        frame = buf[soi:eoi + 2]
                        buf = buf[eoi + 2:]
                        with self._lock:
                            self._latest = frame
                        with self._cond:
                            self._frame_seq += 1
                            self._cond.notify_all()
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if not self._stop.is_set():
                print(f"[cap-{self.cam.id}] ffmpeg exited — restarting in 2s", flush=True)
                time.sleep(2)


# --- Arduino / Servo control -----------------------------------------------
#
# ARDUINO_MODE controls who owns the serial port:
#   "servo"  (default / legacy) — ServoController opens /dev/ttyACM0 and polls
#                                  the old servo+DHT firmware.
#   "bridge" — ServoController does NOT touch the serial port. The separate
#              arduino-bridge service (bridge.py) reads /dev/ttyACM0 exclusively
#              and POSTs sensor data to the backend.
#
# Set ARDUINO_MODE=bridge in the camera-stream env once the new ivc_sensors.ino
# sketch is uploaded and the bridge service is running.

_ARDUINO_MODE = os.environ.get("ARDUINO_MODE", "bridge").lower()

try:
    import serial as _serial
    _SERIAL_OK = True
except ImportError:
    _SERIAL_OK = False


def _find_arduino() -> Optional[str]:
    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


class ServoController:
    OPEN_ANGLE = 90
    CLOSE_ANGLE = 0
    BAUD = 115200

    def __init__(self):
        self._lock = threading.Lock()
        self._ser = None
        self._port = None
        self._feed = self.CLOSE_ANGLE
        self._water = self.CLOSE_ANGLE
        self._temp_c: Optional[float] = None
        self._hum_pct: Optional[float] = None
        self._env_ts: float = 0.0
        self._stop_evt = threading.Event()

        if _ARDUINO_MODE == "bridge":
            # Bridge mode: don't touch the serial port — bridge.py owns it.
            print("[servo] ARDUINO_MODE=bridge — serial port reserved for bridge.py", flush=True)
            return

        self._connect()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="arduino-poll", daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        # Wait a bit so Arduino boot + DHT first-read finishes (~1.5s)
        time.sleep(3)
        while not self._stop_evt.is_set():
            try:
                self.status()
            except Exception as e:
                print(f"[servo] poll error: {e}", flush=True)
            self._stop_evt.wait(5.0)

    def _connect(self):
        if not _SERIAL_OK:
            print("[servo] pyserial not installed — valve control disabled")
            return
        port = _find_arduino()
        if not port:
            print("[servo] No Arduino detected on ttyACM*/ttyUSB* — will retry on demand")
            return
        try:
            self._ser = _serial.Serial(port, self.BAUD, timeout=2)
            self._port = port
            time.sleep(2)
            self._ser.reset_input_buffer()
            print(f"[servo] Arduino connected on {port}")
        except Exception as exc:
            print(f"[servo] Could not open {port}: {exc}")
            self._ser = None

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def _ensure_connected(self):
        if not self.connected:
            self._connect()

    def _send(self, payload: dict) -> Optional[dict]:
        self._ensure_connected()
        if not self.connected:
            return None
        with self._lock:
            try:
                line = json.dumps(payload) + "\n"
                self._ser.write(line.encode())
                resp = self._ser.readline().decode().strip()
                return json.loads(resp) if resp else None
            except Exception as exc:
                print(f"[servo] Serial error: {exc}")
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                return None

    def set_angle(self, servo: str, angle: int) -> dict:
        angle = max(0, min(180, int(angle)))
        resp = self._send({"servo": servo, "angle": angle})
        if resp:
            self._ingest_resp(resp)
        else:
            # Optimistic update so UI stays in sync even without serial response
            if servo == "feed":
                self._feed = angle
            else:
                self._water = angle
        return self.status()

    def pulse(self, servo: str, open_angle: int, duration: float, close_angle: int) -> None:
        def _run():
            self.set_angle(servo, open_angle)
            time.sleep(max(0.1, duration))
            self.set_angle(servo, close_angle)
        threading.Thread(target=_run, daemon=True).start()

    def _ingest_resp(self, resp: Optional[dict]) -> None:
        """Pull all known fields out of an Arduino response into our cache."""
        if not resp:
            return
        self._feed = resp.get("feed", self._feed)
        self._water = resp.get("water", self._water)
        t = resp.get("temp")
        h = resp.get("hum")
        # -127 = Arduino sentinel "no DHT reading yet"; treat as missing
        if isinstance(t, (int, float)) and t > -100:
            self._temp_c = float(t)
        if isinstance(h, (int, float)) and h >= 0:
            self._hum_pct = float(h)
        if (t not in (None, -127)) and (h not in (None, -127)):
            self._env_ts = time.time()

    def status(self) -> dict:
        resp = self._send({"cmd": "status"})
        self._ingest_resp(resp)
        return {"connected": self.connected, "port": self._port,
                "feed": self._feed, "water": self._water}

    def env(self) -> dict:
        """Return latest DHT11 reading. age_s = seconds since last good read
        (None = never read). Caller can decide what's "stale enough"."""
        age = (time.time() - self._env_ts) if self._env_ts else None
        return {
            "connected": self.connected,
            "temperature_c": self._temp_c,
            "humidity_pct": self._hum_pct,
            "age_s": round(age, 1) if age is not None else None,
            "ts": self._env_ts or None,
        }


_servo = ServoController()


# --- RC-toy mouse controller (GPIO -> PC817 optocouplers) ------------------
#
# REWRITTEN FROM ZERO (2026-05-26).
#
# Wiring assumption (active-LOW optocouplers — PC817 cathode-driven):
#   Pi 3.3V --[470R]-- LED anode .. LED cathode -- GPIO pin
#   Pulling GPIO LOW  → current flows → optocoupler ON  → button PRESSED
#   Driving GPIO HIGH → no current     → optocoupler OFF → button RELEASED
#
# This matches the symptom "buttons are still pressing" — GPIO idles LOW
# under the default gpiozero settings, which the original code did, so the
# toy thought every button was held forever.
#
# If your board is actually active-HIGH (opto LED anode tied to GPIO, cathode
# to GND through a resistor), set the env var MOUSE_ACTIVE_LOW=0 to flip it.
#
# State model (deliberately simple — there is no pulse path, no timer, no
# random walk, no shared mutable list of pins):
#   _held: Optional[str]    — which direction is currently pressed (or None)
#   _last_hold_t: float     — monotonic time of the last `hold()` call
#
# A single background thread runs at ~50 Hz. On every tick it computes the
# CORRECT pin state from the two variables above and writes EVERY pin:
#   - If _held is set AND (now - _last_hold_t) < HOLD_WATCHDOG_S:
#       drive _held's pin to ACTIVE, all others to IDLE
#   - Otherwise:
#       drive ALL pins to IDLE
#
# This means: at any moment, the GPIO state is a pure function of two
# variables. There is no path where a pin can be "left HIGH" by a bug — the
# reconciler overwrites the truth 50× per second.

MOUSE_DIRECTIONS: Dict[str, int] = {
    # 2026-06-03 — rewired to pins 11-16 cluster on the header:
    #   Forward (Up)   → GPIO 17  (physical pin 11)
    #   Back (Down)    → GPIO 27  (physical pin 13)
    #   Right          → GPIO 22  (physical pin 15)
    #   Left           → GPIO 23  (physical pin 16)
    "forward": 17,
    "back":    27,
    "right":   22,
    "left":    23,
}
MOUSE_PINS = tuple(MOUSE_DIRECTIONS.values())

# Polarity: CONFIRMED ACTIVE-LOW (2026-05-27 hardware diagnostic).
#
# The /mouse/diag endpoint showed every pin reading "lo" at idle while the
# user's IR controller LED was constantly TX'ing and the toy was spinning
# from all 4 buttons being 'pressed' at once. That's the textbook active-LOW
# symptom: PC817 LED cathode tied to GPIO, anode pulled up via 470Ω. So:
#   GPIO LOW  → current flows through LED → opto ON  → button PRESSED
#   GPIO HIGH → no current                → opto OFF → button RELEASED
#
# Drive the default to active-LOW. Set MOUSE_ACTIVE_LOW=0 only if a future
# board flips the wiring (LED anode driven by GPIO instead).
MOUSE_ACTIVE_LOW = os.environ.get("MOUSE_ACTIVE_LOW", "1") not in ("0", "false", "False")

# How long a hold stays alive after the last refresh. Frontend refreshes
# every 400 ms; 0.6 s gives 1.5 missed refreshes before auto-release.
HOLD_WATCHDOG_S = 0.6
# Reconciler tick — drives the truth onto the pins 50× per second.
RECONCILER_TICK_S = 0.02


class MouseController:
    """4-button RC-toy controller via opto-isolated GPIO.

    PUBLIC API:
      hold(direction)  — start (or refresh) holding a direction. Idempotent.
      release()        — release everything immediately.
      status()         — pin map + which direction (if any) is currently held.

    INVARIANTS:
      - At rest (no direction held, watchdog expired), every pin sits at
        the IDLE level. With MOUSE_ACTIVE_LOW=1 that means GPIO HIGH on
        every pin → opto LED off → buttons released.
      - At most ONE direction can be held at a time. Holding a new one
        implicitly releases the previous.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: Optional[str] = None
        self._last_hold_t: float = 0.0
        self._closed = False
        self._set_pin: Callable[[int, bool], None]
        self._teardown: Callable[[], None]
        # Track the last level we wrote to each pin so the reconciler can
        # SKIP redundant writes. Without this we were toggling 50× per second
        # which (anecdotally on some PC817 setups) can keep the opto LED in a
        # marginal "always-on" state from gate-capacitance / inrush effects.
        self._last_written: Dict[int, Optional[bool]] = {p: None for p in MOUSE_DIRECTIONS.values()}
        # The IDLE level the reconciler writes to "off" pins:
        #   active-low  → IDLE = HIGH (True)
        #   active-high → IDLE = LOW  (False)
        self._idle_level: bool = MOUSE_ACTIVE_LOW
        self._active_level: bool = not MOUSE_ACTIVE_LOW
        self._backend = self._setup_backend()

        # Hammer every pin to IDLE 3× with small sleeps so even a lazy gpio
        # driver actually flushes the write before we start the reconciler.
        for _ in range(3):
            for pin in MOUSE_PINS:
                with suppress(Exception):
                    self._set_pin(pin, self._idle_level)
            time.sleep(0.02)

        atexit.register(self.cleanup)
        # The reconciler is the ONLY thing that writes to pins after init.
        threading.Thread(
            target=self._reconciler_loop,
            name="mouse-reconciler", daemon=True,
        ).start()

        polarity = "active-LOW" if MOUSE_ACTIVE_LOW else "active-HIGH"
        pin_str = " ".join(f"{d}={p}" for d, p in MOUSE_DIRECTIONS.items())
        print(f"[mouse] controller ready (backend={self._backend}, "
              f"polarity={polarity}, pins {pin_str}, "
              f"watchdog={HOLD_WATCHDOG_S}s)", flush=True)

    # ── public API ────────────────────────────────────────────────────────
    def hold(self, direction: str) -> bool:
        """Mark `direction` as held. The reconciler picks this up on its
        next tick (within ~20 ms) and drives the matching pin ACTIVE while
        all others sit at IDLE. Idempotent: re-calling with the same dir
        just refreshes the watchdog deadline."""
        if self._closed or direction not in MOUSE_DIRECTIONS:
            return False
        with self._lock:
            self._held = direction
            self._last_hold_t = time.monotonic()
        return True

    def release(self) -> None:
        """Drop any active hold immediately. The reconciler will set every
        pin to IDLE on the next tick (within ~20 ms). Synchronously sets a
        first pass too so the visible state changes ASAP."""
        with self._lock:
            self._held = None
            for pin in MOUSE_PINS:
                with suppress(Exception):
                    self._set_pin(pin, self._idle_level)

    # Backwards-compatible alias — the Flask /mouse/stop route calls this.
    stop = release

    def status(self) -> dict:
        with self._lock:
            held = self._held
            recent = held is not None and (
                time.monotonic() - self._last_hold_t < HOLD_WATCHDOG_S
            )
        active = {d: (d == held and recent) for d in MOUSE_DIRECTIONS}
        return {
            "backend": self._backend,
            "polarity": "active-low" if MOUSE_ACTIVE_LOW else "active-high",
            "pins": {d: [p] for d, p in MOUSE_DIRECTIONS.items()},
            "active": active,
            "held": held if recent else None,
            "watchdog_s": HOLD_WATCHDOG_S,
            # Kept for frontend backwards-compat:
            "default_pulse_s": 0.0,
            "min_interval_s": 0.0,
            "max_pulse_s": 0.0,
        }

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            self.release()
        with suppress(Exception):
            self._teardown()

    # ── reconciler ────────────────────────────────────────────────────────
    def _reconciler_loop(self) -> None:
        """The only writer to GPIO after init.

        Every RECONCILER_TICK_S, recompute the truth from (_held,
        _last_hold_t) and WRITE EVERY PIN. The 'only write on change'
        optimisation has been removed because diag showed 3 of 4 pins were
        being reset to LOW by the kernel/lgpio between ticks. Writing every
        20 ms means any external reset is reverted within ~20 ms.
        """
        while not self._closed:
            time.sleep(RECONCILER_TICK_S)
            try:
                with self._lock:
                    held = self._held
                    if held is not None and (
                        time.monotonic() - self._last_hold_t >= HOLD_WATCHDOG_S
                    ):
                        # Watchdog: nobody refreshed the hold, drop it.
                        held = None
                        self._held = None
                    target_pin = MOUSE_DIRECTIONS.get(held) if held else None
                    for pin in MOUSE_PINS:
                        level = self._active_level if pin == target_pin else self._idle_level
                        with suppress(Exception):
                            self._set_pin(pin, level)
                        self._last_written[pin] = level
            except Exception as e:
                print(f"[mouse] reconciler error: {e}", flush=True)

    # ── backend setup ─────────────────────────────────────────────────────
    def _setup_backend(self) -> str:
        # gpiozero (preferred on Pi 5 via lgpio) -> RPi.GPIO -> dryrun.
        # We always create the device in active_high=True mode and let our
        # own _idle_level / _active_level handle the polarity inversion —
        # keeps the bookkeeping in one place.
        try:
            from gpiozero import DigitalOutputDevice  # type: ignore[import-not-found]
            devices = {
                pin: DigitalOutputDevice(pin, active_high=True,
                                         initial_value=self._idle_level)
                for pin in MOUSE_PINS
            }
            self._set_pin = lambda pin, high: (
                devices[pin].on() if high else devices[pin].off()
            )

            def _td_gz() -> None:
                for dev in devices.values():
                    with suppress(Exception):
                        dev.off()
                    with suppress(Exception):
                        dev.close()
            self._teardown = _td_gz
            return "gpiozero"
        except Exception as e:
            print(f"[mouse] gpiozero unavailable ({e}) — trying RPi.GPIO", flush=True)
        try:
            import RPi.GPIO as GPIO  # type: ignore[import-not-found]
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            initial = GPIO.HIGH if self._idle_level else GPIO.LOW
            for pin in MOUSE_PINS:
                GPIO.setup(pin, GPIO.OUT, initial=initial)
            self._set_pin = lambda pin, high: GPIO.output(
                pin, GPIO.HIGH if high else GPIO.LOW
            )

            def _td_rpi() -> None:
                for pin in MOUSE_PINS:
                    with suppress(Exception):
                        GPIO.output(pin, initial)
                with suppress(Exception):
                    GPIO.cleanup(list(MOUSE_PINS))
            self._teardown = _td_rpi
            return "rpi.gpio"
        except Exception as e:
            print(f"[mouse] RPi.GPIO unavailable ({e}) — dryrun", flush=True)
        # Dryrun — log only.
        self._set_pin = lambda pin, high: print(
            f"[mouse][dryrun] pin {pin} -> {'HIGH' if high else 'LOW'}",
            flush=True,
        )
        self._teardown = lambda: None
        return "dryrun"


_mouse = MouseController()


# --- Flask app -------------------------------------------------------------

app = Flask(__name__)

_CAMERAS: Dict[str, Camera] = {}
_CAPTURES: Dict[str, CameraCapture] = {}


_CAPTURE_LOCK = threading.Lock()


def _start_captures():
    global _CAMERAS, _CAPTURES
    cams = _v4l2_list()
    _CAMERAS = {c.id: c for c in cams}
    for c in cams:
        if c.id not in _CAPTURES:
            _CAPTURES[c.id] = CameraCapture(c)
    print(f"Started {len(_CAPTURES)} camera capture threads", flush=True)


def _rescan_captures() -> dict:
    """Re-enumerate USB cameras and reconcile the capture threads.

    Lets the dashboard pick up cameras that were plugged in AFTER the service
    started — without an SSH session or a full service restart. Enumeration
    otherwise only happens once at import time, so a hot-plugged camera would
    stay invisible until the next deploy/restart.

    Reconciliation is keyed by USB *bus* (stable across re-enumeration), not by
    the positional id, so a camera that keeps its port keeps its running
    capture thread untouched. New buses get a fresh capture; buses that vanished
    get their thread stopped and dropped.
    """
    global _CAMERAS, _CAPTURES
    with _CAPTURE_LOCK:
        found = _v4l2_list()
        found_by_bus = {c.bus: c for c in found}

        # Map currently-running captures by the bus they were started on.
        running_by_bus = {cap.cam.bus: (cid, cap) for cid, cap in _CAPTURES.items()}

        added, kept, removed = [], [], []

        # Stop captures whose camera was unplugged.
        for bus, (cid, cap) in list(running_by_bus.items()):
            if bus not in found_by_bus:
                cap.stop()
                removed.append(cid)

        # Rebuild the canonical maps from the fresh enumeration, reusing the
        # existing capture thread when the same bus is still present.
        new_cameras: Dict[str, Camera] = {}
        new_captures: Dict[str, CameraCapture] = {}
        for cam in found:
            new_cameras[cam.id] = cam
            prev = running_by_bus.get(cam.bus)
            if prev is not None and cam.bus in found_by_bus:
                _, cap = prev
                cap.cam = cam            # refresh id/name in case position shifted
                new_captures[cam.id] = cap
                kept.append(cam.id)
            else:
                new_captures[cam.id] = CameraCapture(cam)
                added.append(cam.id)

        _CAMERAS = new_cameras
        _CAPTURES = new_captures

    print(f"[cam] rescan: +{len(added)} kept={len(kept)} -{len(removed)} "
          f"(total {len(_CAPTURES)})", flush=True)
    return {"added": added, "kept": kept, "removed": removed,
            "total": len(_CAPTURES)}


_start_captures()


INDEX_HTML = """
<!doctype html>
<title>IVC Cage – Pi Cameras</title>
<style>
 body { font-family: -apple-system, system-ui, sans-serif; background:#0b0f14; color:#e6edf3; margin:0; padding:24px; }
 h1 { font-size:18px; margin:0 0 12px; }
 .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(360px,1fr)); gap:16px; }
 .card { background:#161b22; border:1px solid #30363d; border-radius:8px; overflow:hidden; }
 .head { padding:8px 12px; font-size:13px; border-bottom:1px solid #30363d; }
 .badge { display:inline-block; padding:2px 6px; border-radius:4px; background:#30363d; font-size:11px; margin-left:6px; }
 img { width:100%; display:block; background:#000; }
 .muted { color:#7d8590; }
</style>
<h1>Pi cameras <span class="muted">({{ cams|length }} detected)</span></h1>
<div class="grid">
 {% for c in cams %}
 <div class="card">
   <div class="head">{{ c.name }} <span class="badge">{{ c.device }}</span></div>
   <img src="/stream/{{ c.id }}" alt="{{ c.name }}"/>
 </div>
 {% endfor %}
</div>
"""


@app.get("/")
def index():
    return render_template_string(INDEX_HTML, cams=list(_CAMERAS.values()))


@app.get("/health")
def health():
    alive = sum(1 for c in _CAPTURES.values() if c.get_latest() is not None)
    return jsonify({"status": "ok", "cameras": len(_CAMERAS), "with_frames": alive})


@app.get("/api/cameras")
def list_cameras():
    """List all detected cameras with their capture format and resolution."""
    cams = []
    for c in _CAMERAS.values():
        cap = _CAPTURES.get(c.id)
        cams.append({
            **asdict(c),
            "has_frame":   cap is not None and cap.get_latest() is not None,
            "format":      "mjpeg-passthrough" if c.supports_mjpeg else "yuyv-reencode",
        })
    return jsonify({"cameras": cams})


@app.get("/api/cameras/diag")
def cameras_diag():
    """USB / V4L2 diagnostic — what does the Pi OS actually see at the hardware
    level? Used to debug 'camera service alive but 0 cameras detected' without
    an SSH session. Surfaces:
      - lsusb            : is the camera visible on the USB bus at all?
      - v4l2 devices     : did a /dev/video* node get created?
      - dmesg (usb tail) : enumeration errors, disconnects, brown-outs
      - throttled        : Pi undervoltage flag (0x0 = healthy). 3× 1080p IR
                           cameras can exceed the Pi's USB budget without a
                           powered hub, and undervolt drops devices off the bus.
    """
    def _run(cmd):
        try:
            return subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                           timeout=5).decode(errors="replace").strip()
        except Exception as exc:
            return f"error: {exc}"

    lsusb = _run(["lsusb"])
    v4l2  = _run(["v4l2-ctl", "--list-devices"])
    video_nodes = _run(["bash", "-c", "ls -1 /dev/video* 2>/dev/null || echo '(none)'"])
    dmesg_usb = _run(["bash", "-c",
                      "dmesg 2>/dev/null | grep -iE 'usb|uvc|video' | tail -25 "
                      "|| echo '(dmesg needs root — try sudo)'"])
    throttled = _run(["bash", "-c",
                      "vcgencmd get_throttled 2>/dev/null || echo '(vcgencmd n/a)'"])

    # Heuristic hint for the most common failure modes.
    hint = None
    low = lsusb.lower()
    if "over-current" in dmesg_usb.lower() or "overcurrent" in dmesg_usb.lower():
        hint = ("⚠ USB OVER-CURRENT: the Pi cut power to its USB ports to protect "
                "itself — that's why no camera enumerates (lsusb shows only root "
                "hubs). 3× 1080p IR cameras + IR LED arrays exceed the Pi 5's USB "
                "current budget (~1.6 A total). FIX: connect every camera through "
                "the externally-powered USB hub, NOT directly into the Pi. The hub's "
                "own PSU supplies the camera current; the Pi ports then carry data only.")
    elif "error" in v4l2.lower() and "no such" in v4l2.lower():
        hint = "v4l2-ctl missing — install with: sudo apt install v4l-utils"
    elif video_nodes == "(none)" and ("camera" in low or "webcam" in low or "uvc" in low):
        hint = ("Camera is on the USB bus (lsusb) but no /dev/video node — "
                "uvcvideo driver may not have bound. Check dmesg_usb.")
    elif video_nodes == "(none)":
        hint = ("No camera on the USB bus at all. Check: cable seated, camera "
                "plugged into the PI (not the host PC), and use the POWERED USB "
                "hub — 3× 1080p IR cameras exceed the Pi's bus current budget.")
    if "throttled=0x" in throttled and throttled.split("throttled=")[-1] not in ("0x0", "0x0\n"):
        hint = (hint or "") + " ⚠ Pi reports undervoltage/throttling — use a stronger PSU + powered hub."

    return jsonify({
        "lsusb":       lsusb,
        "v4l2_list":   v4l2,
        "video_nodes": video_nodes,
        "dmesg_usb":   dmesg_usb,
        "throttled":   throttled,
        "detected_by_service": len(_CAMERAS),
        "hint":        hint,
    })


@app.route("/api/cameras/rescan", methods=["POST", "OPTIONS"])
def rescan_cameras():
    """Hot-plug rescan: re-detect USB cameras and (re)start capture threads.

    Call this after physically connecting a camera so it shows up without
    restarting the service or SSHing into the Pi. Returns which camera ids
    were added / kept / removed."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    result = _rescan_captures()
    return jsonify({"ok": True, **result,
                    "cameras": [asdict(c) for c in _CAMERAS.values()]})


@app.post("/api/cameras/<cam_id>/reinit")
def reinit_camera(cam_id: str):
    """Re-apply V4L2 IR controls to a camera (useful after camera reconnect
    or if exposure drifted).  Does not restart the capture thread."""
    cam = _CAMERAS.get(cam_id)
    if cam is None:
        abort(404)
    _v4l2_init_ir(cam.device)
    return jsonify({"ok": True, "cam_id": cam_id, "device": cam.device})


# --- V4L2 camera controls (brightness, contrast, exposure, …) ---------------
#
# These three routes expose the camera's full V4L2 control surface over HTTP.
# They are generic — they parse whatever `v4l2-ctl --list-ctrls-menus` reports
# rather than hard-coding a fixed list.  That means they work across different
# camera models without changes.
#
# ffmpeg / the capture thread are NOT touched — v4l2-ctl writes directly to the
# open V4L2 device node and Linux applies the changes live to the running stream.


def _v4l2_list_controls(device: str) -> List[dict]:
    """Run v4l2-ctl --list-ctrls-menus and parse into a list of control dicts.

    Supported line shapes (output of v4l2-ctl 1.x):
      Section headers (skipped):
        "User Controls"
        "Camera Controls"
        ""

      Control entry:
        "                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0"
        "        white_balance_automatic 0x0098090c (bool)   : default=1 value=1"
        "       power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1 (50 Hz)"

      Menu option lines following a menu control:
        "\t\t\t0: Disabled"
        "\t\t\t1: 50 Hz"
    """
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", f"--device={device}", "--list-ctrls-menus"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode(errors="replace")
    except Exception as exc:
        raise RuntimeError(f"v4l2-ctl failed: {exc}") from exc

    controls: List[dict] = []
    current: Optional[dict] = None

    for raw_line in out.splitlines():
        # ── Menu option line ─────────────────────────────────────────────────
        # Starts with one or more tabs then a digit, colon, space, label.
        # e.g. "\t\t\t0: Disabled"
        m_opt = re.match(r"^\t+(\d+)\s*:\s+(.+)$", raw_line)
        if m_opt and current and current.get("type") == "menu":
            current.setdefault("menu", []).append(
                {"value": int(m_opt.group(1)), "label": m_opt.group(2).strip()}
            )
            continue

        # ── Control entry line ───────────────────────────────────────────────
        # Pattern: leading spaces, name, 0x address, (type), colon, key=val pairs.
        m_ctrl = re.match(
            r"^\s+(\w+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s+(.+)$",
            raw_line,
        )
        if m_ctrl:
            name  = m_ctrl.group(1)
            ctype = m_ctrl.group(2)   # int / bool / menu / button
            rest  = m_ctrl.group(3)

            def _kv(key: str) -> Optional[int]:
                m = re.search(rf"\b{key}=(-?\d+)", rest)
                return int(m.group(1)) if m else None

            ctrl: dict = {
                "name":    name,
                "type":    ctype,
                "min":     _kv("min"),
                "max":     _kv("max"),
                "step":    _kv("step"),
                "default": _kv("default"),
                "value":   _kv("value"),
                "menu":    [],
            }
            controls.append(ctrl)
            current = ctrl
            continue

        # ── Section header or blank ──────────────────────────────────────────
        # These lines have no leading whitespace that matches a control, or are
        # blank.  Unset `current` so stray indented lines don't attach to the
        # wrong control.
        stripped = raw_line.strip()
        if not stripped or re.match(r"^[A-Z][A-Za-z ]+$", stripped):
            current = None

    # Remove empty menu lists for non-menu controls to keep the payload tidy.
    for ctrl in controls:
        if ctrl["type"] != "menu":
            ctrl.pop("menu", None)

    return controls


def _v4l2_get_control(device: str, name: str) -> Optional[int]:
    """Read back a single control value. Returns None on error."""
    try:
        out = subprocess.check_output(
            ["v4l2-ctl", f"--device={device}", f"--get-ctrl={name}"],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode(errors="replace")
        # Output: "name: 42\n"
        m = re.search(r":\s*(-?\d+)", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


@app.route("/camera/<cam_id>/controls", methods=["GET", "POST", "OPTIONS"])
def camera_controls(cam_id: str):
    """GET  /camera/<cam_id>/controls — list all V4L2 controls for this camera.
    POST /camera/<cam_id>/controls — set a single V4L2 control.

    GET response:
      {"device": "/dev/videoN", "controls": [{
          "name": "brightness", "type": "int",
          "min": -64, "max": 64, "step": 1, "default": 0, "value": 12
      }, ...]}
    Menu controls also include a "menu" key:
      {"name": "power_line_frequency", "type": "menu", ...,
       "menu": [{"value": 0, "label": "Disabled"}, {"value": 1, "label": "50 Hz"}, ...]}

    POST body: {"name": "brightness", "value": 12}
    POST validation:
      - control must exist for this camera (400 otherwise)
      - int:  value validated to [min, max]
      - menu: value must be a valid option index
      - bool: value must be 0 or 1
    POST returns: {"ok": true, "name": "brightness", "value": <readback>}
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200

    cam = _CAMERAS.get(cam_id)
    if cam is None:
        abort(404)

    # ── GET ──────────────────────────────────────────────────────────────────
    if request.method == "GET":
        try:
            ctrls = _v4l2_list_controls(cam.device)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify({"device": cam.device, "controls": ctrls})

    # ── POST ─────────────────────────────────────────────────────────────────
    body = request.get_json(silent=True) or {}
    ctrl_name = body.get("name")
    ctrl_val  = body.get("value")
    if not isinstance(ctrl_name, str) or ctrl_name == "":
        return jsonify({"ok": False, "error": "body must have {\"name\": str, \"value\": int}"}), 400
    if ctrl_val is None or not isinstance(ctrl_val, (int, float)):
        return jsonify({"ok": False, "error": "\"value\" must be a number"}), 400
    ctrl_val = int(ctrl_val)

    # Fetch the current control list so we can validate name + value range.
    try:
        ctrls = _v4l2_list_controls(cam.device)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    ctrl_map = {c["name"]: c for c in ctrls}
    if ctrl_name not in ctrl_map:
        known = ", ".join(sorted(ctrl_map.keys()))
        return jsonify({
            "ok": False,
            "error": f"unknown control '{ctrl_name}'; known: {known}",
        }), 400

    ctrl = ctrl_map[ctrl_name]
    ctype = ctrl.get("type", "int")

    if ctype == "bool":
        if ctrl_val not in (0, 1):
            return jsonify({"ok": False,
                            "error": f"'{ctrl_name}' is bool; value must be 0 or 1"}), 400
    elif ctype == "menu":
        valid_vals = {opt["value"] for opt in (ctrl.get("menu") or [])}
        if valid_vals and ctrl_val not in valid_vals:
            opts = ", ".join(f"{o['value']}={o['label']}"
                             for o in (ctrl.get("menu") or []))
            return jsonify({"ok": False,
                            "error": f"'{ctrl_name}' menu value {ctrl_val} invalid; "
                                     f"options: {opts}"}), 400
    elif ctype in ("int", "integer"):
        lo = ctrl.get("min")
        hi = ctrl.get("max")
        if lo is not None and hi is not None:
            if not (lo <= ctrl_val <= hi):
                return jsonify({"ok": False,
                                "error": f"'{ctrl_name}' value {ctrl_val} out of range "
                                         f"[{lo}, {hi}]"}), 400
    # button type: no value constraint needed

    # Apply the control.
    try:
        subprocess.run(
            ["v4l2-ctl", f"--device={cam.device}",
             f"--set-ctrl={ctrl_name}={ctrl_val}"],
            capture_output=True, timeout=4,
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"v4l2-ctl set failed: {exc}"}), 500

    # Read back the value to confirm it was applied.
    readback = _v4l2_get_control(cam.device, ctrl_name)
    return jsonify({"ok": True, "name": ctrl_name, "value": readback})


@app.route("/camera/<cam_id>/controls/reset", methods=["POST", "OPTIONS"])
def camera_controls_reset(cam_id: str):
    """POST /camera/<cam_id>/controls/reset — reset every control to its default.

    Iterates all controls returned by v4l2-ctl and sets each to its reported
    default value.  Controls with no default (None) are skipped.  Does NOT
    restart ffmpeg — the changes apply live to the open device node.

    Returns the refreshed controls list (same shape as GET /camera/<cam_id>/controls).
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    cam = _CAMERAS.get(cam_id)
    if cam is None:
        abort(404)
    try:
        ctrls = _v4l2_list_controls(cam.device)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    errors: List[str] = []
    for ctrl in ctrls:
        default = ctrl.get("default")
        if default is None:
            continue
        try:
            subprocess.run(
                ["v4l2-ctl", f"--device={cam.device}",
                 f"--set-ctrl={ctrl['name']}={default}"],
                capture_output=True, timeout=4,
            )
        except Exception as exc:
            errors.append(f"{ctrl['name']}: {exc}")

    # Re-apply the IR-optimised baseline (manual exposure ~6ms, gain 16, AWB off)
    # on top of the driver defaults. Without this, "reset" would restore the
    # camera's bright factory exposure (~16ms) and the IR image washes out white.
    try:
        _v4l2_init_ir(cam.device)
    except Exception as exc:
        errors.append(f"ir_baseline: {exc}")

    # Re-read the list so returned values reflect the hardware state.
    try:
        ctrls = _v4l2_list_controls(cam.device)
    except RuntimeError:
        pass  # return whatever we have

    return jsonify({
        "ok":      True,
        "device":  cam.device,
        "controls": ctrls,
        "errors":  errors,
    })


@app.get("/stream/<cam_id>")
def stream(cam_id: str):
    cap = _CAPTURES.get(cam_id)
    if cap is None:
        abort(404)

    def gen() -> Iterator[bytes]:
        boundary = b"--frame"
        # Wait briefly for the first frame
        deadline = time.time() + 3
        while cap.get_latest() is None and time.time() < deadline:
            time.sleep(0.1)
        last_seq = -1
        while True:
            frame = cap.wait_next(timeout=2.0)
            if frame is None:
                continue
            yield (
                boundary
                + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(frame)).encode()
                + b"\r\n\r\n"
                + frame
                + b"\r\n"
            )

    return Response(
        gen(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                 "X-Accel-Buffering": "no"},
    )


@app.get("/snapshot/<cam_id>")
def snapshot(cam_id: str):
    cap = _CAPTURES.get(cam_id)
    if cap is None:
        abort(404)
    # Wait up to 2s for a frame if we don't have one yet (capture warming up)
    frame = cap.get_latest()
    if frame is None:
        deadline = time.time() + 2
        while frame is None and time.time() < deadline:
            time.sleep(0.1)
            frame = cap.get_latest()
    if frame is None:
        abort(503)
    return Response(frame, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/snapshot/<cam_id>/next")
def snapshot_next(cam_id: str):
    """Long-polling snapshot: block until a frame newer than ?since=N arrives.

    Why: through a Cloudflare tunnel each plain /snapshot request costs ~50-150 ms
    RTT. Polling every 80 ms wastes RTT and caps fps to whatever the tunnel can
    serve. With long-polling the server holds the connection open until the
    next frame is captured (typically <33 ms at 30 fps), then flushes it
    immediately. The client re-requests the moment the response arrives,
    so frames flow back-to-back with zero idle gap.

    Headers:
      X-Frame-Seq: monotonically increasing frame number — pass the previous
                   value back as ?since=N to get only newer frames.
    """
    cap = _CAPTURES.get(cam_id)
    if cap is None:
        abort(404)
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    # Cap long-poll timeout to ~25 s so CF tunnels' 100s idle limit never bites.
    frame, seq = cap.wait_after(since, timeout=25.0)
    if frame is None:
        # 204 = "no new frame within the timeout" — client just retries.
        return Response(status=204, headers={"X-Frame-Seq": str(seq)})
    return Response(
        frame,
        mimetype="image/jpeg",
        headers={
            "Cache-Control":                 "no-cache, no-store, must-revalidate",
            "X-Frame-Seq":                   str(seq),
            "Access-Control-Expose-Headers": "X-Frame-Seq",
        },
    )


@app.get("/servo/status")
def servo_status():
    """Return servo + pump status. In bridge mode, sends a status query to the
    Arduino over serial and returns the response. Falls back to cached state."""
    if _ARDUINO_MODE == "bridge":
        import pathlib
        ports = sorted(pathlib.Path("/dev").glob("ttyACM*"))
        connected = len(ports) > 0
        return jsonify({"connected": connected,
                        "port": str(ports[0]) if ports else None,
                        "feed": _bridge_servo["feed"], "water": _bridge_servo["water"],
                        "pump_on": False})
    return jsonify(_servo.status())


# Shared env state — updated by the bridge OR the ServoController, whichever
# has fresher data. The bridge reads the Arduino's new sensor sketch (which
# includes temp/hum), so this works even when ServoController can't parse it.
_bridge_env: dict = {"temperature_c": None, "humidity_pct": None, "ts": 0.0}

# Last servo angles we relayed to the bridge. In bridge mode the camera server
# doesn't own the serial port, so it can't query the Arduino directly — but the
# firmware obeys the commanded angle, so caching what we sent lets /servo/status
# reflect the real open/closed state (otherwise the dashboard is stuck on 0 =
# "Closed" no matter what the user does).
_bridge_servo: dict = {"feed": 0, "water": 0}


@app.route("/sensor/env/update", methods=["POST", "OPTIONS"])
def sensor_env_update():
    """Called by the bridge to push fresh DHT11 values into the camera service.
    This way /sensor/env always returns the latest reading regardless of
    whether the ServoController can parse the Arduino output."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body = request.get_json(silent=True) or {}
    t = body.get("temperature_c")
    h = body.get("humidity_pct")
    if isinstance(t, (int, float)) and t > -100:
        _bridge_env["temperature_c"] = float(t)
    if isinstance(h, (int, float)) and h >= 0:
        _bridge_env["humidity_pct"] = float(h)
    if t is not None or h is not None:
        _bridge_env["ts"] = time.time()
    return jsonify({"ok": True})


@app.get("/sensor/env")
def sensor_env():
    """Latest DHT11 reading. Prefers bridge data (new sketch), falls back to
    ServoController (old firmware). The dashboard header widget polls this."""
    servo_data = _servo.env()
    # If bridge has fresher data, use it
    bridge_age = time.time() - _bridge_env["ts"] if _bridge_env["ts"] else None
    servo_age = servo_data.get("age_s")

    if _bridge_env["ts"] and (bridge_age is not None and bridge_age < 30):
        return jsonify({
            "connected": True,
            "temperature_c": _bridge_env["temperature_c"],
            "humidity_pct": _bridge_env["humidity_pct"],
            "age_s": round(bridge_age, 1),
            "ts": _bridge_env["ts"],
        })
    return jsonify(servo_data)


# ── Open-loop time-based dosing ──────────────────────────────────────────────
# The flow sensor (D2) emits ~200 phantom pulses/s of electrical noise, so the
# firmware's closed-loop {"dose":mL} cuts the pump almost immediately; and the
# water load cell doesn't track the dose reservoir. With both feedback sensors
# unusable (and the cage closed, so no hardware fix yet), we dose OPEN-LOOP: run
# the pump for ml / rate seconds. The rate is tunable via the DOSE_RATE_ML_S env
# var or a per-request "rate"; a hard cap bounds the volume, and each pump burst
# auto-stops in firmware so a stuck thread can't run it dry.
# Calibrated 2026-06-19: commanding 1 mL at the old 0.5 rate (a 2 s run) put
# ~9 mL on the scale, i.e. the pump actually moves ~4.5 mL/s. Setting the rate to
# the measured value makes commanded mL ≈ delivered mL.
_DOSE_RATE_ML_S = float(os.environ.get("DOSE_RATE_ML_S", "4.5"))   # mL per second (measured)
_DOSE_MAX_S     = float(os.environ.get("DOSE_MAX_S", "180"))       # cap on one dose (s)
_PUMP_BURST_S   = 28.0   # firmware caps {"pump":"on","dur"} at 30s — re-issue under that

_dose_stop = threading.Event()
_dose_thread: Optional[threading.Thread] = None
_dose_lock = threading.Lock()


def _write_cmd(cmd: dict) -> None:
    with open(_CMD_FILE, "w") as f:
        f.write(json.dumps(cmd) + "\n")


def _wait_cmd_consumed(timeout: float = 2.5) -> None:
    """Block until the bridge has picked up (deleted) the queued command. The
    cmd file is a single-slot mailbox the bridge polls only every ~2 s; without
    this, a short dose's pump-on write was clobbered by the trailing pump-off
    before the bridge ever read it, so the pump never ran. Bounded so a stuck
    bridge can't hang the dose thread."""
    t0 = time.monotonic()
    while os.path.exists(_CMD_FILE) and (time.monotonic() - t0) < timeout:
        time.sleep(0.05)


def _cancel_dose() -> None:
    """Signal any in-progress timed dose to stop and wait for it to wind down."""
    _dose_stop.set()
    t = _dose_thread
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=3.0)


def _run_timed_dose(duration_s: float, stop_evt: threading.Event) -> None:
    """Keep the pump running for ~duration_s by re-issuing timed pump bursts
    (each within the firmware's 30s cap), then stop. A monotonic end-time keeps
    the total run time on target regardless of burst overlap."""
    end = time.monotonic() + duration_s
    try:
        while not stop_evt.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0.05:
                break
            burst = min(_PUMP_BURST_S, remaining)
            # Send the real burst length. Floor at 150 ms so a short dose still
            # runs (a sub-mL dose is a fraction of a second now that the rate is
            # ~4.5 mL/s) AND stays clear of the firmware's "dur < 100 ms → 5 s"
            # safety clamp. Don't floor to 1 s — that made small doses overshoot.
            dur_ms = max(150, int(burst * 1000))
            _write_cmd({"pump": "on", "dur": dur_ms})
            # Wait until the bridge relays this burst before doing anything that
            # would overwrite the mailbox (the next burst, or the final off).
            # The firmware auto-stops the pump after dur_ms, so the burst length
            # itself is firmware-timed and exact regardless of relay latency.
            _wait_cmd_consumed()
            # Re-issue ~6s before the burst lapses (covers cmd-file relay latency)
            # so the pump stays continuous; let the final burst run out on its own.
            wait_for = burst if burst >= remaining - 0.1 else burst - 6.0
            waited = 0.0
            while waited < wait_for and not stop_evt.is_set():
                time.sleep(0.1)
                waited += 0.1
    finally:
        _write_cmd({"pump": "off"})
        _wait_cmd_consumed()


def _start_timed_dose(ml: float, rate_ml_s: float) -> float:
    """Cancel any running dose and start a new open-loop timed dose. Returns the
    planned pump duration in seconds."""
    global _dose_thread
    with _dose_lock:
        _cancel_dose()
        rate = rate_ml_s if rate_ml_s and rate_ml_s > 0 else _DOSE_RATE_ML_S
        duration_s = min(ml / rate, _DOSE_MAX_S)
        _dose_stop.clear()
        _dose_thread = threading.Thread(
            target=_run_timed_dose, args=(duration_s, _dose_stop),
            name="timed-dose", daemon=True)
        _dose_thread.start()
        return duration_s


@app.route("/pump", methods=["POST", "OPTIONS"])
def pump_set():
    """Water pump control.

    Dosing is OPEN-LOOP (time-based): the flow sensor is too noisy and the water
    load cell doesn't track the dose, so {"dose":mL} runs the pump for mL / rate
    seconds rather than metering by sensor. Accuracy depends on the rate
    (DOSE_RATE_ML_S, default 0.5 mL/s; override per-request with "rate").

    Body (one of):
        {"dose": 15}                 → run the pump ~15 mL worth of time
        {"dose": 15, "rate": 0.4}    → same, with a one-off rate override
        {"pump": "on", "dur": 5000}  → run pump for 5 s
        {"pump": "off"}              → stop pump (and cancel any timed dose)
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if _ARDUINO_MODE != "bridge":
        return jsonify({"ok": False, "error": "pump control requires ARDUINO_MODE=bridge"}), 503
    body = request.get_json(silent=True) or {}
    try:
        if "dose" in body:
            ml = float(body["dose"])
            if not (0 < ml <= 1000):
                return jsonify({"ok": False, "error": "dose must be 0–1000 mL"}), 400
            rate = float(body.get("rate") or _DOSE_RATE_ML_S)
            seconds = _start_timed_dose(ml, rate)
            return jsonify({"ok": True, "mode": "timed", "ml": ml,
                            "rate_ml_s": rate, "seconds": round(seconds, 1)})
        elif body.get("pump") == "on":
            _cancel_dose()
            dur = int(body.get("dur", 5000))
            dur = max(100, min(30000, dur))
            _write_cmd({"pump": "on", "dur": dur})
            return jsonify({"ok": True, "queued": {"pump": "on", "dur": dur}})
        elif body.get("pump") == "off":
            _cancel_dose()
            _write_cmd({"pump": "off"})
            return jsonify({"ok": True, "queued": {"pump": "off"}})
        else:
            return jsonify({"ok": False, "error": "expected {\"dose\":mL} or {\"pump\":\"on|off\"}"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:120]}), 500


@app.route("/servo/<servo_name>", methods=["POST", "OPTIONS"])
def servo_set(servo_name: str):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if servo_name not in ("feed", "water"):
        abort(404)
    body = request.get_json(silent=True) or {}
    angle = body.get("angle")
    pulse_duration = body.get("pulse")
    if angle is None:
        abort(400)

    if _ARDUINO_MODE == "bridge":
        # Write servo command to the command queue file — bridge picks it up
        try:
            _bridge_servo[servo_name] = int(angle)
            cmd = json.dumps({"servo": servo_name, "angle": int(angle)})
            with open(_CMD_FILE, "w") as f:
                f.write(cmd + "\n")
            if pulse_duration is not None:
                # Schedule a close command after the pulse
                def _delayed_close():
                    time.sleep(max(0.1, float(pulse_duration)))
                    _bridge_servo[servo_name] = 0
                    with open(_CMD_FILE, "w") as f:
                        f.write(json.dumps({"servo": servo_name, "angle": 0}) + "\n")
                threading.Thread(target=_delayed_close, daemon=True).start()
                return jsonify({"status": "pulsing", "servo": servo_name,
                                "angle": angle, "duration": pulse_duration})
            return jsonify({"connected": True,
                            "feed": _bridge_servo["feed"], "water": _bridge_servo["water"]})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:120]}), 500

    if pulse_duration is not None:
        _servo.pulse(servo_name, int(angle), float(pulse_duration), ServoController.CLOSE_ANGLE)
        return jsonify({"status": "pulsing", "servo": servo_name,
                        "angle": angle, "duration": pulse_duration})
    result = _servo.set_angle(servo_name, int(angle))
    return jsonify(result)


# --- Mouse (RC-toy) control endpoints --------------------------------------

@app.get("/mouse/status")
def mouse_status():
    """Backend in use, pin map, and which pins are currently pulsing."""
    return jsonify(_mouse.status())


@app.post("/mouse/diag/pinctrl-force/<int:pin>/<state>")
def mouse_diag_pinctrl_force(pin: int, state: str):
    """Drive a single pin via the `pinctrl` CLI directly, completely bypassing
    gpiozero/lgpio. Used to determine whether a stuck pin is a library bug or
    a hardware fault. state ∈ {'hi', 'lo'}."""
    if state not in ("hi", "lo"):
        abort(400)
    drive = "dh" if state == "hi" else "dl"
    try:
        subprocess.check_output(
            ["pinctrl", "set", str(pin), "op", drive],
            stderr=subprocess.STDOUT, timeout=2,
        )
        time.sleep(0.05)
        readback = subprocess.check_output(
            ["pinctrl", "get", str(pin)],
            stderr=subprocess.STDOUT, timeout=2,
        ).decode().strip()
        return jsonify({"pin": pin, "wrote": state, "readback": readback})
    except Exception as e:
        return jsonify({"pin": pin, "wrote": state, "error": str(e)}), 500


@app.get("/mouse/diag")
def mouse_diag():
    """Real-GPIO diagnostic: ask `pinctrl` what each mouse pin is ACTUALLY
    doing at the hardware level, plus list every process that has the
    gpiochip char-device open. If the toy is moving despite /mouse/status
    saying all pins are idle, this endpoint will reveal who's driving them."""
    out: dict = {
        "software_view": _mouse.status(),
        "hardware_view": {},
        "gpiochip_users": [],
    }
    # 1. pinctrl get N  → e.g. "17: op pn hi // GPIO17 = output, drive HIGH"
    for direction, pin in MOUSE_DIRECTIONS.items():
        try:
            r = subprocess.check_output(
                ["pinctrl", "get", str(pin)],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode().strip()
            out["hardware_view"][direction] = {"pin": pin, "raw": r}
        except Exception as e:
            out["hardware_view"][direction] = {"pin": pin, "error": str(e)}
    # 2. Who has /dev/gpiochip0 open? fuser is the most reliable tool.
    try:
        r = subprocess.check_output(
            ["fuser", "/dev/gpiochip0", "/dev/gpiochip4"],
            stderr=subprocess.STDOUT, timeout=2,
        ).decode().strip()
        out["gpiochip_users"] = r.split()
    except subprocess.CalledProcessError as e:
        out["gpiochip_users"] = e.output.decode().strip().split() if e.output else []
    except Exception as e:
        out["gpiochip_users"] = [f"error: {e}"]
    # 3. All python processes on the box — quick triage.
    try:
        r = subprocess.check_output(
            ["pgrep", "-a", "-f", "python"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode().splitlines()
        out["python_processes"] = r
    except Exception as e:
        out["python_processes"] = [f"error: {e}"]
    return jsonify(out)


@app.route("/mouse/stop", methods=["POST", "OPTIONS"])
def mouse_stop():
    """Force every mouse pin LOW immediately. The frontend calls this on
    every button release (pointer-up / pointer-leave / pointer-cancel)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    _mouse.stop()
    return jsonify({"ok": True, "action": "stop"})


@app.route("/mouse/hold/<action>", methods=["POST", "OPTIONS"])
def mouse_hold(action: str):
    """Hold a direction pin HIGH. Call on button-press and every ~400 ms
    while held. GPIO auto-releases 1.5 s after the last call (watchdog).
    Send POST /mouse/stop to release immediately on button-up.

    This is the ONLY way to drive a pin HIGH — the old pulse / random-walk
    endpoints have been removed so nothing can move the toy except a held
    button on the dashboard."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if action not in {"forward", "back", "left", "right"}:
        abort(404)
    ok = _mouse.hold(action)
    return jsonify({"ok": ok, "action": action, "held": ok})


# Stub: keep old /mouse/random/* and /mouse/<action> POSTs from 502-ing if
# anything stale calls them. They all just return "removed" and force a stop
# so callers can't accidentally drive the toy via legacy paths.
@app.route("/mouse/random/start", methods=["POST", "OPTIONS"])
@app.route("/mouse/random/stop",  methods=["POST", "OPTIONS"])
@app.route("/mouse/random",       methods=["POST", "OPTIONS"])
@app.route("/mouse/forward",      methods=["POST", "OPTIONS"])
@app.route("/mouse/back",         methods=["POST", "OPTIONS"])
@app.route("/mouse/left",         methods=["POST", "OPTIONS"])
@app.route("/mouse/right",        methods=["POST", "OPTIONS"])
def mouse_legacy_removed():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    # Anything that hits a removed endpoint also gets a hard stop so we
    # never leave a pin HIGH because of a stale client.
    _mouse.stop()
    return jsonify({
        "ok": False,
        "removed": True,
        "message": "Endpoint removed. Use POST /mouse/hold/<dir> + /mouse/stop.",
    }), 410


# --- System control endpoints -------------------------------------------------
# CPU temp, memory, fan speed (read + write), reboot, shutdown.
# All state reads come from /proc or /sys so they work on any Pi without
# extra Python packages.
#
# Fan persistence: Linux's thermal daemon continuously rewrites the fan's
# cooling_device sysfs node.  To make a non-Max level actually stick we run
# our own keep-alive thread that re-writes the desired level every 2 s —
# the same trick used by fan-max.service, but for any level 0-3.
# "Auto" mode (level -1) stops our thread and lets the thermal daemon decide.
# Level 4 delegates to fan-max.service (stops our thread first).

_fan_target: Optional[int] = None      # None = auto, 0-3 = persistent level
_fan_stop_evt = threading.Event()
_fan_thread: Optional[threading.Thread] = None

# Persistence: store the user's chosen level so it survives ivc-cameras
# restarts (every pi-deploy.sh run restarts the service; without this file
# the keep-alive thread starts in 'None' and fan-max.service wins by default,
# spinning the fan back up to max every time we deploy).
#
# /var/tmp/ is world-writable on the Pi, survives reboots (unlike /tmp/),
# and doesn't need sudo.
_FAN_STATE_FILE = "/var/tmp/ivc-fan-target"


def _fan_state_save(target: Optional[int]) -> None:
    """Persist the desired fan target.  '4' = Max (fan-max.service in charge),
    '-1' = Auto (let thermal daemon decide), 0-3 = our keep-alive at that level.
    None means "don't override anything"."""
    try:
        if target is None:
            try:
                os.remove(_FAN_STATE_FILE)
            except FileNotFoundError:
                pass
        else:
            with open(_FAN_STATE_FILE, "w") as f:
                f.write(f"{int(target)}\n")
    except OSError as e:
        print(f"[fan] could not persist state to {_FAN_STATE_FILE}: {e}", flush=True)


def _fan_state_load() -> Optional[int]:
    """Read the last-set fan target from disk.  Returns None if the file
    doesn't exist or is malformed (fresh boot, first time, etc)."""
    try:
        with open(_FAN_STATE_FILE) as f:
            raw = f.read().strip()
        v = int(raw)
        if -1 <= v <= 4:
            return v
    except (OSError, ValueError):
        pass
    return None


# Auto-mode thermal curve. At level L the fan steps UP to L+1 when temp ≥ UP[L],
# and steps DOWN to L-1 when temp ≤ DOWN[L]. DOWN sits ~5°C below the UP that
# got us there, so the fan doesn't oscillate at a threshold boundary. Mirrors
# the Pi 5's own trip points (50/60/67.5/75°C) but managed by us, because the
# kernel governor was observed to leave cur_state parked at 4 after fan-max
# hammered it.
_FAN_AUTO_UP   = {0: 50.0, 1: 60.0, 2: 67.5, 3: 75.0}
_FAN_AUTO_DOWN = {1: 45.0, 2: 55.0, 3: 62.0, 4: 70.0}
_fan_auto_cur  = 0   # remembered auto-curve level (hysteresis state)


def _fan_write_level(lvl: int) -> None:
    """Write a fan level (0-4) to the cooling device + every hwmon pwm1 node."""
    pwm_val = round(lvl / 4 * 255)
    _sysfs_write("/sys/class/thermal/cooling_device0/cur_state", str(lvl))
    for pwm_path in glob.glob("/sys/class/hwmon/hwmon*/pwm1"):
        _sysfs_write(pwm_path, str(pwm_val))


def _fan_auto_next(temp: Optional[float], cur: int) -> int:
    """One hysteresis step of the auto curve from level `cur` given `temp`."""
    if temp is None:
        return max(cur, 2)                       # can't read temp → safe-ish
    if cur < 4 and temp >= _FAN_AUTO_UP[cur]:
        return cur + 1
    if cur > 0 and temp <= _FAN_AUTO_DOWN[cur]:
        return cur - 1
    return cur


def _fan_keep_alive() -> None:
    """Background thread driving the fan.

    target == -1  → AUTO: read CPU temp every 3 s and walk the hysteresis curve.
    target 0-3    → FIXED: re-write the level every 100 ms so nothing else
                    (a stray write, a respawned service) can steal it.
    target None   → idle (thread about to exit).

    Writes to *every* hwmon pwm1 node — some Pi 5 configs expose more than one.
    """
    global _fan_auto_cur
    while not _fan_stop_evt.is_set():
        tgt = _fan_target
        if tgt == -1:
            _fan_auto_cur = _fan_auto_next(_pi_cpu_temp(), _fan_auto_cur)
            _fan_write_level(_fan_auto_cur)
            _fan_stop_evt.wait(3.0)
        elif tgt is not None:
            _fan_write_level(tgt)
            _fan_stop_evt.wait(0.1)
        else:
            _fan_stop_evt.wait(0.2)


def _fan_thread_start(level: int) -> None:
    """Start (or restart) the fan thread at the given level (-1 auto, 0-3 fixed)."""
    global _fan_thread, _fan_target, _fan_auto_cur
    _fan_stop_evt.clear()
    _fan_target = level
    if level == -1:
        # Seed the auto curve from the current hardware level so it converges
        # immediately instead of stepping all the way from 0.
        cur_raw = _sysfs_read("/sys/class/thermal/cooling_device0/cur_state")
        try:
            _fan_auto_cur = int(cur_raw) if cur_raw is not None else 2
        except ValueError:
            _fan_auto_cur = 2
    if _fan_thread is None or not _fan_thread.is_alive():
        _fan_thread = threading.Thread(target=_fan_keep_alive, daemon=True, name="fan-keep-alive")
        _fan_thread.start()


def _fan_thread_stop() -> None:
    """Stop the keep-alive thread (hand control back to daemon or fan-max.service)."""
    global _fan_target
    _fan_target = None
    _fan_stop_evt.set()


def _fan_restore_on_boot() -> None:
    """On service startup, re-apply the user's last-chosen fan level.
    Without this, every ivc-cameras restart drops the keep-alive thread and
    fan-max.service spins the fan straight back up to max."""
    target = _fan_state_load()
    if target is None:
        return
    print(f"[fan] restoring persisted target = {target}", flush=True)
    if target == 4:
        # Max is handled by fan-max.service — nothing to run on our side.
        return
    if target == -1:
        # Auto: run our own thermal curve (kernel governor is unreliable here).
        _fan_thread_start(-1)
        return
    # 0-3: fixed level — seed the hardware then hold it with the keep-alive thread.
    _fan_write_level(target)
    _fan_thread_start(target)

def _sysfs_read(path: str) -> Optional[str]:
    """Read a single-value sysfs/proc file. Returns stripped string or None."""
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _sysfs_write(path: str, value: str) -> bool:
    """Write to a sysfs file; fall back to `sudo -n tee` if permission denied."""
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except PermissionError:
        try:
            subprocess.run(
                ["sudo", "-n", "tee", path],
                input=value.encode(), check=True, timeout=2,
                stdout=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False
    except Exception:
        return False


def _pi_fan_info() -> dict:
    level_raw = _sysfs_read("/sys/class/thermal/cooling_device0/cur_state")
    max_raw   = _sysfs_read("/sys/class/thermal/cooling_device0/max_state")
    pwm: Optional[int] = None
    for pwm_path in glob.glob("/sys/class/hwmon/hwmon*/pwm1"):
        val = _sysfs_read(pwm_path)
        try:
            pwm = int(val) if val is not None else None
        except ValueError:
            pass
        break
    level     = int(level_raw) if level_raw is not None else None
    max_level = int(max_raw)   if max_raw   else 4
    return {
        "level":     level,
        "max_level": max_level,
        "pwm":       pwm,
        "percent":   round(level / max_level * 100) if (level is not None and max_level) else None,
    }


def _pi_cpu_temp() -> Optional[float]:
    raw = _sysfs_read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw) / 1000.0, 1) if raw else None
    except (ValueError, TypeError):
        return None


def _pi_cpu_percent() -> Optional[float]:
    """Compute CPU % from /proc/stat over a 150 ms window (blocks briefly)."""
    def _stat():
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
            vals  = list(map(int, parts))
            idle  = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            return idle, sum(vals)
        except Exception:
            return None, None
    i1, t1 = _stat()
    time.sleep(0.15)
    i2, t2 = _stat()
    if None in (i1, t1, i2, t2):
        return None
    dt = t2 - t1  # type: ignore[operator]
    return round((1 - (i2 - i1) / dt) * 100, 1) if dt else 0.0  # type: ignore[operator]


def _pi_memory() -> dict:
    try:
        mem: dict = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                try:
                    mem[k.strip()] = int(v.split()[0])   # kB
                except (ValueError, IndexError):
                    pass
        total = mem.get("MemTotal",     0)
        avail = mem.get("MemAvailable", 0)
        used  = total - avail
        return {
            "total_mb":     round(total / 1024),
            "used_mb":      round(used  / 1024),
            "available_mb": round(avail / 1024),
            "percent":      round(used / total * 100, 1) if total else 0,
        }
    except Exception:
        return {}


def _pi_uptime() -> Optional[float]:
    raw = _sysfs_read("/proc/uptime")
    try:
        return float(raw.split()[0]) if raw else None
    except (ValueError, AttributeError):
        return None


@app.get("/system/status")
def system_status():
    """Pi hardware metrics — CPU temp, CPU load, memory, fan level, uptime."""
    return jsonify({
        "cpu_percent": _pi_cpu_percent(),
        "cpu_temp_c":  _pi_cpu_temp(),
        "memory":      _pi_memory(),
        "fan":         _pi_fan_info(),
        "uptime_s":    _pi_uptime(),
    })


@app.get("/system/fan/diag")
def system_fan_diag():
    """Find out what is actually holding the fan at a given level — used when
    Auto mode is set but the fan stays pinned at max. Surfaces fan-max.service
    state plus every process writing to the fan sysfs nodes (orphaned
    `while true` loops survive `systemctl stop` because they're outside the
    service cgroup)."""
    def _run(cmd):
        try:
            return subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                           timeout=5).decode(errors="replace").strip()
        except Exception as exc:
            return f"error: {exc}"

    is_active  = _run(["sudo", "-n", "systemctl", "is-active", "fan-max.service"])
    is_enabled = _run(["sudo", "-n", "systemctl", "is-enabled", "fan-max.service"])
    # Any process whose command line touches the fan sysfs nodes.
    fan_procs = _run(["bash", "-c",
                      "ps -eo pid,ppid,user,args | grep -iE "
                      "'cur_state|pwm1|fan-max|cooling_device' | grep -v grep "
                      "|| echo '(none)'"])
    cur_state = _sysfs_read("/sys/class/thermal/cooling_device0/cur_state")
    trips = _run(["bash", "-c",
                  "for t in /sys/class/thermal/thermal_zone0/trip_point_*_temp; do "
                  "echo \"$t = $(cat $t 2>/dev/null)\"; done 2>/dev/null "
                  "|| echo '(no trip points)'"])
    return jsonify({
        "fan_max_active":  is_active,
        "fan_max_enabled": is_enabled,
        "fan_writers":     fan_procs,
        "cur_state":       cur_state,
        "our_target":      _fan_target,
        "trip_points":     trips,
        "fan":             _pi_fan_info(),
    })


@app.route("/system/fan", methods=["POST", "OPTIONS"])
def system_fan():
    """Set fan cooling level.
      -1  = Auto  — stop all overrides; let the Pi thermal daemon decide.
       0  = Off   — keep-alive thread holds fan at 0 (beats the daemon).
       1-3 = Low/Med/High — keep-alive thread holds the level.
       4  = Max   — delegate to fan-max.service (persistent max).
    Requires: JSON body {"level": -1..4}
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body  = request.get_json(silent=True) or {}
    level = body.get("level")
    if level is None or not isinstance(level, int) or not (-1 <= level <= 4):
        return jsonify({"ok": False, "error": "level must be an int -1 (auto) or 0–4"}), 400

    errors: list = []
    diag: dict = {}

    def _sysctl(*args: str) -> tuple[int, str]:
        try:
            r = subprocess.run(["sudo", "-n", "systemctl", *args],
                               capture_output=True, timeout=4)
            return r.returncode, r.stderr.decode("utf-8", "replace")[:200]
        except Exception as e:
            return -1, str(e)[:200]

    def _neuter_fan_max(disable: bool) -> tuple[bool, str]:
        """Stop fan-max.service from forcing the fan to max.

        PRIMARY mechanism = `systemctl disable` (proven passwordless on this Pi
        — the same `sudo -n systemctl` that `stop` uses). systemd does NOT honour
        Restart=always after an explicit `systemctl stop`, so stop+disable holds
        both now AND across reboots (disable removes the multi-user.target.wants
        symlink so it won't auto-start on boot).

        SECONDARY (best-effort) = a drop-in override that strips Restart=always
        and ExecStart. This needs `sudo mkdir`/`sudo tee`, which may not be
        passwordless — if so we just skip it. The disable path is sufficient on
        its own, so a drop-in failure is NOT treated as fatal.

        Returns (ok, detail). ok=True if the primary (disable/enable) succeeded.
        """
        notes = []
        if disable:
            # Primary: disable so it never auto-starts (boot or otherwise).
            rc = subprocess.run(["sudo", "-n", "systemctl", "disable", "fan-max.service"],
                                capture_output=True, timeout=4)
            primary_ok = rc.returncode == 0
            if not primary_ok:
                notes.append(f"disable rc={rc.returncode}: {rc.stderr.decode()[:80]}")
            # Best-effort drop-in (only works where sudo mkdir/tee are allowed).
            override_dir  = "/etc/systemd/system/fan-max.service.d"
            override_file = f"{override_dir}/ivc-disable.conf"
            body = "[Service]\nExecStart=\nExecStart=/bin/true\nRestart=no\nRestartSec=86400\n"
            mk = subprocess.run(["sudo", "-n", "mkdir", "-p", override_dir],
                                capture_output=True, timeout=3)
            if mk.returncode == 0:
                tee = subprocess.run(["sudo", "-n", "tee", override_file],
                                     input=body.encode(), capture_output=True, timeout=3)
                notes.append("drop-in installed" if tee.returncode == 0
                             else f"tee skipped ({tee.stderr.decode()[:60]})")
            else:
                notes.append("drop-in skipped (no sudo mkdir)")
        else:
            # Re-enable for Max mode.
            rc = subprocess.run(["sudo", "-n", "systemctl", "enable", "fan-max.service"],
                                capture_output=True, timeout=4)
            primary_ok = rc.returncode == 0
            if not primary_ok:
                notes.append(f"enable rc={rc.returncode}: {rc.stderr.decode()[:80]}")
            subprocess.run(["sudo", "-n", "rm", "-f",
                            "/etc/systemd/system/fan-max.service.d/ivc-disable.conf"],
                           capture_output=True, timeout=3)
        # Reload so systemd picks up the disable/enable + any drop-in change.
        subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"],
                       capture_output=True, timeout=3)
        return primary_ok, "; ".join(notes)

    # ── FAST PATH (instant, no subprocess) ──────────────────────────────────
    # Apply the effect the user will SEE immediately: persist the choice and
    # (re)start our keep-alive / auto-curve thread, which writes the fan level
    # at 100 ms intervals. This is what actually controls the fan, and it takes
    # microseconds. We return to the client right after this.
    #
    # The SLOW work — systemctl disable/enable/stop/start + mkdir/tee/daemon-
    # reload to neuter fan-max.service — runs in a BACKGROUND thread. Those
    # calls can take several seconds and, through the Cloudflare tunnel, used to
    # blow past the browser's fetch timeout → "Fan control failed" toast even
    # though the Pi applied the change. Deferring them keeps the endpoint <50 ms.
    _fan_state_save(level)
    _fan_thread_stop()

    if level == 4:
        _fan_write_level(4)
    elif level == -1:
        _fan_thread_start(-1)          # auto thermal curve
    else:
        _fan_write_level(level)
        _fan_thread_start(level)       # hold this fixed level

    def _reconcile_fan_max() -> None:
        """Background: stop fan-max from competing (or re-enable it for Max).
        Failures here are non-fatal — the keep-alive thread already dominates."""
        try:
            if level == 4:
                _neuter_fan_max(disable=False)
                _sysctl("start", "fan-max.service")
            else:
                _neuter_fan_max(disable=True)
                _sysctl("stop", "fan-max.service")
        except Exception as exc:
            print(f"[fan] background reconcile error: {exc}", flush=True)

    threading.Thread(target=_reconcile_fan_max, daemon=True,
                     name="fan-reconcile").start()

    # Return instantly — the fan is already being driven to `level`.
    pwm_val = round(level / 4 * 255) if level >= 0 else 0
    return jsonify({
        "ok":      True,
        "level":   level,
        "pwm":     pwm_val,
        "applied": "immediate; fan-max reconcile running in background",
        "fan":     _pi_fan_info(),
    })


@app.route("/system/reboot", methods=["POST", "OPTIONS"])
def system_reboot():
    """Reboot the Pi.  Body must contain {"confirm": "reboot"} for safety."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "reboot":
        return jsonify({"ok": False, "error": 'Send {"confirm": "reboot"}'}), 400

    def _do() -> None:
        time.sleep(1.5)
        subprocess.run(["sudo", "-n", "reboot"], check=False)
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "action": "reboot", "message": "Rebooting in ~1.5 s…"})


@app.route("/system/shutdown", methods=["POST", "OPTIONS"])
def system_shutdown():
    """Shut down the Pi.  Body must contain {"confirm": "shutdown"} for safety."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "shutdown":
        return jsonify({"ok": False, "error": 'Send {"confirm": "shutdown"}'}), 400

    def _do() -> None:
        time.sleep(1.5)
        subprocess.run(["sudo", "-n", "shutdown", "-h", "now"], check=False)
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "action": "shutdown", "message": "Shutting down in ~1.5 s…"})


# --- Arduino bridge setup (remote, no SSH needed) -----------------------------

@app.route("/system/arduino-upload", methods=["POST", "OPTIONS"])
def arduino_upload():
    """Compile and upload ivc_sensors.ino to the Arduino Mega from the Pi.

    Installs arduino-cli if needed, then compiles and uploads the sketch
    that's already rsynced to ~/IndustryProject/device/arduino/ivc_sensors/.
    This replaces the old servo firmware with the new multi-sensor sketch.

    No args needed — it uses the known paths.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200

    import pathlib, shutil
    home = pathlib.Path.home()
    sketch_dir = home / "IndustryProject" / "device" / "arduino" / "ivc_sensors"
    cli_bin    = home / "bin" / "arduino-cli"
    notes      = []
    errors     = []

    def _run(cmd, timeout=120):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               env={**os.environ, "HOME": str(home)})
            return r.returncode, r.stdout.decode(errors="replace"),\
                   r.stderr.decode(errors="replace")
        except Exception as e:
            return -1, "", str(e)

    # 0. Force a git pull so the sketch is definitely at HEAD, then verify.
    repo_dir = home / "IndustryProject"
    _run(["git", "-C", str(repo_dir), "fetch", "--quiet", "origin", "main"], timeout=30)
    _run(["git", "-C", str(repo_dir), "reset", "--hard", "origin/main"], timeout=10)
    ino = sketch_dir / "ivc_sensors.ino"
    # Clean stale files (old .cpp/.h from previous commit may confuse arduino-cli)
    for stale in sketch_dir.glob("*"):
        if stale.name not in ("ivc_sensors.ino",) and stale.suffix in (".cpp", ".h", ".yaml"):
            stale.unlink(missing_ok=True)
            notes.append(f"removed stale: {stale.name}")
    if not ino.exists():
        return jsonify({"ok": False,
                        "error": f"Sketch not found at {ino} — repo structure broken"}), 404
    lines = len(ino.read_text().splitlines())
    sha_rc, sha_out, _ = _run(["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"], timeout=5)
    notes.append(f"sketch: {ino} ({lines} lines, git={sha_out.strip()})")

    # 1. Install arduino-cli if missing
    if not cli_bin.exists():
        notes.append("installing arduino-cli...")
        rc, out, err = _run(["bash", "-c",
            f"mkdir -p {home}/bin && "
            f"curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | "
            f"BINDIR={home}/bin sh"], timeout=120)
        if rc != 0:
            return jsonify({"ok": False, "error": f"arduino-cli install failed: {err[:300]}"}), 500
        notes.append("arduino-cli installed")
    else:
        notes.append("arduino-cli already installed")

    # 2. Install Arduino Mega core + HX711 library
    # Pi 5 is aarch64 — the default arduino:avr core ships x86 avr-gcc binaries
    # that fail with "exec format error". We must also install avr-gcc natively
    # via apt so the Pi's own ARM toolchain is used.
    notes.append("ensuring arm avr-gcc + core + HX711 lib...")
    _run(["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends",
          "avr-libc", "avrdude", "gcc-avr"], timeout=120)
    _run([str(cli_bin), "core", "update-index"], timeout=60)
    rc, _, err = _run([str(cli_bin), "core", "install", "arduino:avr"], timeout=180)
    if rc != 0:
        notes.append(f"core install warn: {err[:200]}")
    rc, _, err = _run([str(cli_bin), "lib", "install", "HX711 Arduino Library"], timeout=60)
    if rc != 0:
        notes.append(f"lib install warn: {err[:200]}")
    # The Pi is aarch64 — the Arduino bundled avr-gcc is x86 and won't run.
    # BUT: the system avr-gcc (apt) is too new (gcc 14 vs Arduino's gcc 7)
    # and causes linker errors with the Arduino core.
    # Solution: nuke the old core, reinstall using arduino-cli's built-in
    # ARM-aware download, which grabs the correct aarch64 toolchain.
    avr_tools = home / ".arduino15" / "packages" / "arduino" / "tools" / "avr-gcc"
    need_reinstall = False
    if avr_tools.exists():
        for td in sorted(avr_tools.iterdir()):
            test_bin = td / "bin" / "avr-g++"
            if test_bin.exists() and not test_bin.is_symlink():
                rc2, out2, _ = _run(["file", str(test_bin)], timeout=3)
                if "x86" in out2 or "Intel" in out2:
                    need_reinstall = True
            elif test_bin.is_symlink():
                # Previously symlinked to system — wrong gcc version. Remove.
                need_reinstall = True

    if need_reinstall:
        notes.append("removing x86/mismatched avr-gcc, reinstalling ARM toolchain...")
        import shutil as _shutil
        _shutil.rmtree(str(avr_tools), ignore_errors=True)
        # Also clean ctags
        ctags_tools = home / ".arduino15" / "packages" / "builtin" / "tools" / "ctags"
        _shutil.rmtree(str(ctags_tools), ignore_errors=True)
        # Reinstall — arduino-cli 0.35+ auto-detects aarch64 and downloads ARM binaries
        _run([str(cli_bin), "core", "install", "arduino:avr", "--force"], timeout=300)
        notes.append("arduino:avr reinstalled (ARM-native)")
    else:
        notes.append("avr-gcc toolchain OK")

    # Install ctags from apt as fallback (arduino-cli's ctags may still be x86)
    ctags_path = home / ".arduino15" / "packages" / "builtin" / "tools" / "ctags"
    if ctags_path.exists():
        for exe in ctags_path.rglob("ctags"):
            if exe.is_file() and not exe.is_symlink():
                rc2, out2, _ = _run(["file", str(exe)], timeout=3)
                if "x86" in out2 or "Intel" in out2:
                    _run(["sudo", "-n", "apt-get", "install", "-y",
                          "--no-install-recommends", "universal-ctags"], timeout=60)
                    sys_ctags = shutil.which("ctags")
                    if sys_ctags:
                        exe.unlink(missing_ok=True)
                        exe.symlink_to(sys_ctags)
                        notes.append("ctags → system")

    # 3. Stop bridge so it releases the serial port during upload
    uenv = _user_env()
    subprocess.run(["systemctl", "--user", "stop", "ivc-arduino-bridge"],
                   env=uenv, capture_output=True, timeout=5)
    time.sleep(1)
    notes.append("bridge stopped for upload")

    # 4. Find the Arduino port
    import glob as _glob
    acm = sorted(_glob.glob("/dev/ttyACM*"))
    if not acm:
        errors.append("no /dev/ttyACM* device found — is Arduino plugged in?")
        return jsonify({"ok": False, "errors": errors, "notes": notes}), 404
    port = acm[0]
    notes.append(f"upload port: {port}")

    # 5. Compile (--build-property to disable Arduino auto-prototypes)
    notes.append("compiling...")
    rc, out, err = _run([
        str(cli_bin), "compile",
        "--fqbn", "arduino:avr:mega",
        "--build-property", "compiler.cpp.extra_flags=-DARDUINO_MAIN",
        str(sketch_dir),
    ], timeout=180)
    if rc != 0:
        errors.append(f"compile failed:\n{out[-500:]}\n{err[-500:]}")
        # Restart bridge even on failure
        subprocess.run(["systemctl", "--user", "start", "ivc-arduino-bridge"],
                       env=uenv, capture_output=True, timeout=5)
        return jsonify({"ok": False, "errors": errors, "notes": notes}), 500
    notes.append("compile OK")

    # 6. Upload
    notes.append("uploading to Arduino...")
    rc, out, err = _run([
        str(cli_bin), "upload",
        "--fqbn", "arduino:avr:mega",
        "--port", port,
        str(sketch_dir),
    ], timeout=60)
    if rc != 0:
        errors.append(f"upload failed:\n{out[-300:]}\n{err[-300:]}")
    else:
        notes.append("upload OK — new sensor firmware running")

    # 7. Restart bridge
    time.sleep(2)
    subprocess.run(["systemctl", "--user", "start", "ivc-arduino-bridge"],
                   env=uenv, capture_output=True, timeout=5)
    notes.append("bridge restarted")

    return jsonify({
        "ok": len(errors) == 0,
        "errors": errors,
        "notes": notes,
    })


# ── Arduino command endpoint (write to serial) ──────────────────────────────
# Sends a raw byte/string to the Arduino over /dev/ttyACM0. The bridge reads
# sensor data; this endpoint writes commands (pump on/off, tare, servo, etc.).
# Uses a one-shot serial open so it doesn't conflict with the bridge's read loop.

# Command queue file — the bridge watches this and relays commands to Arduino.
# Using a file avoids opening a second serial connection (which conflicts with
# the bridge's read loop and causes "device disconnected" errors).
_CMD_FILE = "/tmp/ivc-arduino-cmd"

@app.route("/system/arduino/cmd", methods=["POST", "OPTIONS"])
def arduino_cmd():
    """Send a command to the Arduino via the bridge's command queue.

    Body: {"cmd": "p"}           → single char command (p=pump, x=stop, t=tare)
    Body: {"cmd": "json", "json": {"servo":"feed","angle":90}}  → JSON command
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body = request.get_json(silent=True) or {}
    cmd = body.get("cmd", "")
    if not cmd:
        return jsonify({"ok": False, "error": "cmd required"}), 400

    try:
        if cmd == "json":
            payload = json.dumps(body.get("json", {}))
        else:
            payload = cmd
        with open(_CMD_FILE, "w") as f:
            f.write(payload + "\n")
        return jsonify({"ok": True, "sent": payload})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500


@app.route("/system/inference/stop", methods=["POST", "OPTIONS"])
def inference_stop():
    """Stop on-Pi inference entirely (for when detection runs via Colab instead).
    Kills any in-progress pip/torch BUILD that's pegging the CPU, stops+disables
    ivc-inference (no crash-loop), and restarts ivc-backend to clear any
    CPU-starvation hang so Colab's POSTs to /api/v1/ai/detections succeed."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    uenv = _user_env()
    actions: dict = {}

    # 1. Kill the heavy build (pip + the C/C++ compilers it spawns).
    for pat in ("python3 -m pip", "pip install", "pip._internal",
                "cc1plus", "cc1", "ninja", "inference.py"):
        r = subprocess.run(["pkill", "-9", "-f", pat], capture_output=True, timeout=5)
        actions[pat] = r.returncode  # 0 = killed something, 1 = nothing matched

    # 2. Stop + disable the inference service so it can't restart/compete.
    for args in (["stop", "ivc-inference"], ["disable", "ivc-inference"]):
        subprocess.run(["systemctl", "--user", *args], env=uenv,
                       capture_output=True, timeout=12)

    # 3. Restart the backend to clear any hang from the CPU starvation.
    rb = subprocess.run(["systemctl", "--user", "restart", "ivc-backend"],
                        env=uenv, capture_output=True, timeout=20)
    actions["backend_restart_rc"] = rb.returncode

    return jsonify({
        "ok": True,
        "actions": actions,
        "note": "Pi build killed, inference disabled, backend restarted. "
                "Run detection via Colab — its POSTs should now succeed.",
    })


@app.route("/system/inference/install-deps", methods=["POST", "OPTIONS"])
def inference_install_deps():
    """Install the YOLO inference deps into /usr/bin/python3 (the interpreter
    the ivc-inference unit runs) in a BACKGROUND thread. Heavy (~400 MB torch
    ARM) so it can't complete within an HTTP timeout — returns immediately and
    writes progress to ~/pi-inference/pip-install.log. Poll /system/inference/deps."""
    if request.method == "OPTIONS":
        return jsonify({}), 200
    import pathlib
    inf_dir = pathlib.Path.home() / "pi-inference"
    req = inf_dir / "requirements.txt"
    pip_log = inf_dir / "pip-install.log"
    if not req.is_file():
        return jsonify({"ok": False, "error": f"{req} missing"}), 404

    # Already installed?
    probe = subprocess.run(["/usr/bin/python3", "-c", "import ultralytics, cv2, numpy"],
                           capture_output=True, timeout=20)
    if probe.returncode == 0:
        return jsonify({"ok": True, "already_installed": True})

    def _install():
        try:
            with open(pip_log, "w") as f:
                f.write("=== pip install starting ===\n")
                f.flush()
                subprocess.run(
                    ["/usr/bin/python3", "-m", "pip", "install",
                     "--break-system-packages", "-r", str(req)],
                    stdout=f, stderr=subprocess.STDOUT, timeout=1200,
                )
                f.write("\n=== pip install finished ===\n")
            # Restart inference now that deps are present
            subprocess.run(["systemctl", "--user", "restart", "ivc-inference"],
                           env=_user_env(), capture_output=True, timeout=15)
        except Exception as exc:
            try:
                with open(pip_log, "a") as f:
                    f.write(f"\n=== install error: {exc} ===\n")
            except Exception:
                pass

    threading.Thread(target=_install, daemon=True, name="pip-inference").start()
    return jsonify({"ok": True, "started": True,
                    "note": "Installing torch+opencv+ultralytics (~5-10 min). Poll /system/inference/deps."})


@app.get("/system/inference/deps")
def inference_deps_status():
    """Check whether the inference deps are importable + tail the pip log."""
    import pathlib
    probe = subprocess.run(["/usr/bin/python3", "-c",
                            "import ultralytics, cv2, numpy; print('ok')"],
                           capture_output=True, timeout=20)
    ready = probe.returncode == 0
    pip_log = pathlib.Path.home() / "pi-inference" / "pip-install.log"
    tail: list = []
    try:
        if pip_log.is_file():
            tail = pip_log.read_text(errors="replace").strip().splitlines()[-15:]
    except Exception:
        pass
    return jsonify({
        "deps_ready": ready,
        "probe_err": probe.stderr.decode(errors="replace")[:200] if not ready else "",
        "pip_log_tail": tail,
    })


@app.route("/system/inference/setup", methods=["POST", "OPTIONS"])
def inference_setup():
    """One-shot remote setup for the on-Pi YOLO mouse detector (ivc-inference).
    Writes ~/pi-inference/.env with the API key, ensures the systemd unit is
    installed, then enables + starts it. The service auto-exports Fine_Tuned.pt
    → NCNN on first run and begins POSTing detections to the Live AI panel.

    Body: {"api_key": "ivc_...", "backend_url": "https://example.org",
           "conf": 0.4, "imgsz": 480}
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body    = request.get_json(silent=True) or {}
    api_key = body.get("api_key", "")
    backend = body.get("backend_url", "https://example.org")
    conf    = body.get("conf", 0.40)
    imgsz   = body.get("imgsz", 480)
    if not api_key.startswith("ivc_"):
        return jsonify({"ok": False, "error": "api_key must start with ivc_"}), 400

    import pathlib
    inf_dir  = pathlib.Path.home() / "pi-inference"
    env_file = inf_dir / ".env"
    uenv     = _user_env()
    notes    = []

    if not inf_dir.is_dir():
        return jsonify({"ok": False,
                        "error": f"{inf_dir} not found — deploy hasn't rsynced pi-inference yet"}), 404

    try:
        env_file.write_text(
            f"API_KEY={api_key}\n"
            f"BACKEND_URL={backend}\n"
            f"CAMERA_URL=http://127.0.0.1:8090\n"
            f"CONF_THRESH={conf}\n"
            f"YOLO_IMGSZ={imgsz}\n"
            f"USE_NCNN=1\n"
        )
        notes.append(f".env written to {env_file}")
    except Exception as exc:
        return jsonify({"ok": False, "error": f"could not write .env: {exc}"}), 500

    # Which model will it use?
    for cand in ("Fine_Tuned.pt", "best.pt"):
        if (inf_dir / cand).is_file():
            notes.append(f"model present: {cand}")
            break

    # Ensure systemd unit installed
    unit_src = inf_dir / "ivc-inference.service"
    unit_dst = pathlib.Path.home() / ".config/systemd/user/ivc-inference.service"
    if unit_src.is_file():
        unit_dst.parent.mkdir(parents=True, exist_ok=True)
        if not unit_dst.is_file() or unit_dst.read_text() != unit_src.read_text():
            import shutil as _sh
            _sh.copy(str(unit_src), str(unit_dst))
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           env=uenv, timeout=5, capture_output=True)
            notes.append("ivc-inference.service installed/updated")

    errs = []
    for args in [["enable", "ivc-inference"], ["restart", "ivc-inference"]]:
        r = subprocess.run(["systemctl", "--user", *args],
                           env=uenv, capture_output=True, timeout=12)
        if r.returncode != 0:
            errs.append(f"{args[0]}: {r.stderr.decode()[:120]}")
        else:
            notes.append(f"systemctl {args[0]}: ok")

    time.sleep(2)
    state = subprocess.run(["systemctl", "--user", "is-active", "ivc-inference"],
                           env=uenv, capture_output=True, timeout=5).stdout.decode().strip()

    return jsonify({
        "ok": len(errs) == 0,
        "service_state": state,
        "notes": notes,
        "errors": errs,
        "tip": "First start exports the model to NCNN (~30-60s for a fine-tuned model) before detections appear.",
    })


@app.get("/system/inference/status")
def inference_status():
    """Live status of the YOLO detector: service state + recent log lines.
    The unit logs to ~/pi-inference/inference.log (file), so read that."""
    import pathlib
    uenv  = _user_env()
    state = subprocess.run(["systemctl", "--user", "is-active", "ivc-inference"],
                           env=uenv, capture_output=True, timeout=5).stdout.decode().strip()
    log_path = pathlib.Path.home() / "pi-inference" / "inference.log"
    lines: list = []
    try:
        if log_path.is_file():
            lines = log_path.read_text(errors="replace").strip().splitlines()[-40:]
    except Exception as e:
        lines = [f"(could not read inference.log: {e})"]
    return jsonify({"service_state": state, "log_tail": lines})


@app.route("/system/arduino-mode", methods=["POST", "OPTIONS"])
def set_arduino_mode():
    """Switch between 'servo' (legacy) and 'bridge' (new sensor sketch) mode.

    When mode=bridge, the ServoController releases /dev/ttyACM0 so the
    arduino-bridge service can read it exclusively. Takes effect on the
    next ivc-cameras restart (which this endpoint triggers automatically).

    Body: {"mode": "bridge"} or {"mode": "servo"}
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "bridge")
    if mode not in ("bridge", "servo"):
        return jsonify({"ok": False, "error": "mode must be 'bridge' or 'servo'"}), 400

    import pathlib
    # Write to a persistent env-snippet that the systemd unit picks up.
    # The ivc-cameras.service reads EnvironmentFile=~/camera-stream/env if present.
    env_path = pathlib.Path.home() / "camera-stream" / "env"
    try:
        # Read existing env, replace or add ARDUINO_MODE line
        existing = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()
        existing["ARDUINO_MODE"] = mode
        env_path.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    except Exception as exc:
        return jsonify({"ok": False, "error": f"could not write env: {exc}"}), 500

    # Restart camera service so it picks up the new mode
    uenv = _user_env()
    # Kill stale python first (same pattern as pi-deploy.sh)
    subprocess.run(["pkill", "-9", "-f", "python3.*server\\.py"],
                   capture_output=True, timeout=3)
    time.sleep(1)
    r = subprocess.run(["systemctl", "--user", "restart", "ivc-cameras"],
                       env=uenv, capture_output=True, timeout=10)

    return jsonify({
        "ok": r.returncode == 0,
        "mode": mode,
        "env_path": str(env_path),
        "restart_rc": r.returncode,
        "note": f"ServoController will {'NOT touch' if mode == 'bridge' else 'open'} /dev/ttyACM0",
    })


@app.route("/system/arduino-bridge/setup", methods=["POST", "OPTIONS"])
def arduino_bridge_setup():
    """One-shot remote setup for the Arduino serial bridge.

    Creates ~/arduino-bridge/.env with the supplied API key and cage ID,
    installs Python deps, then enables + starts the systemd user service.
    Safe to call repeatedly (idempotent).

    Body: {"api_key": "ivc_...", "cage_id": "cage-001", "backend_url": "https://example.org"}
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200

    body      = request.get_json(silent=True) or {}
    api_key   = body.get("api_key",      "")
    cage_id   = body.get("cage_id",      "cage-001")
    backend   = body.get("backend_url",  "https://example.org")

    if not api_key.startswith("ivc_"):
        return jsonify({"ok": False,
                        "error": "api_key must start with ivc_"}), 400

    import pathlib
    bridge_dir  = pathlib.Path.home() / "arduino-bridge"
    env_file    = bridge_dir / ".env"
    unit_dst    = pathlib.Path.home() / ".config/systemd/user/ivc-arduino-bridge.service"
    uenv        = _user_env()
    notes       = []

    # 1. Create .env (overwrite so we always have latest key)
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        env_file.write_text(
            f"API_KEY={api_key}\n"
            f"CAGE_ID={cage_id}\n"
            f"BACKEND_URL={backend}\n"
            f"SERIAL_PORT=/dev/ttyACM0\n"
            f"BAUD=115200\n"
        )
        notes.append(f".env written to {env_file}")
    except Exception as exc:
        return jsonify({"ok": False, "error": f"could not write .env: {exc}"}), 500

    # 2. Install Python deps if not already present
    req_file = bridge_dir / "requirements.txt"
    if req_file.exists():
        try:
            r = subprocess.run(
                ["pip", "install", "--quiet", "--break-system-packages",
                 "-r", str(req_file)],
                capture_output=True, timeout=60,
            )
            notes.append("pip install ok" if r.returncode == 0
                         else f"pip warn: {r.stderr.decode()[:200]}")
        except Exception as exc:
            notes.append(f"pip skipped: {exc}")

    # 3. Check Arduino is visible on USB
    acm_devs = sorted(pathlib.Path("/dev").glob("ttyACM*"))
    notes.append(f"Arduino USB devices: {[str(p) for p in acm_devs]}")

    # 4. Install systemd unit if not already there
    unit_src = bridge_dir / "ivc-arduino-bridge.service"
    if unit_src.exists() and not unit_dst.exists():
        try:
            unit_dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy(str(unit_src), str(unit_dst))
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           env=uenv, timeout=5, capture_output=True)
            notes.append("systemd unit installed")
        except Exception as exc:
            notes.append(f"unit install warn: {exc}")

    # 5. Enable + (re)start the service
    errs = []
    for args in [["enable", "ivc-arduino-bridge"],
                 ["restart", "ivc-arduino-bridge"]]:
        r = subprocess.run(["systemctl", "--user", *args],
                           env=uenv, capture_output=True, timeout=10)
        if r.returncode != 0:
            errs.append(f"{args[0]}: {r.stderr.decode()[:120]}")
        else:
            notes.append(f"systemctl {args[0]}: ok")

    # 6. Read back service status + first log lines
    time.sleep(2)
    state = subprocess.run(
        ["systemctl", "--user", "is-active", "ivc-arduino-bridge"],
        env=uenv, capture_output=True, timeout=5,
    ).stdout.decode().strip()

    log_lines = ""
    try:
        log_path = pathlib.Path.home() / "arduino-bridge" / "bridge.log"
        if log_path.exists():
            log_lines = log_path.read_text(errors="replace").strip().splitlines()[-20:]
    except Exception:
        pass

    return jsonify({
        "ok":           len(errs) == 0,
        "service_state": state,
        "notes":        notes,
        "errors":       errs,
        "log_tail":     log_lines,
        "arduino_usb":  [str(p) for p in acm_devs],
    })


@app.get("/system/arduino-bridge/status")
def arduino_bridge_status():
    """Live status: service state + last 30 log lines + serial port check."""
    import pathlib
    uenv     = _user_env()
    state    = subprocess.run(
        ["systemctl", "--user", "is-active", "ivc-arduino-bridge"],
        env=uenv, capture_output=True, timeout=5,
    ).stdout.decode().strip()
    acm_devs = sorted(pathlib.Path("/dev").glob("ttyACM*"))
    log_lines: list = []
    try:
        log_path = pathlib.Path.home() / "arduino-bridge" / "bridge.log"
        if log_path.exists():
            log_lines = log_path.read_text(errors="replace").strip().splitlines()[-30:]
    except Exception:
        pass
    return jsonify({
        "service_state": state,
        "arduino_usb":   [str(p) for p in acm_devs],
        "log_tail":      log_lines,
    })


# --- Backend-service diagnostics (emergency remote access) --------------------

def _user_env() -> dict:
    """Build an env dict that has XDG_RUNTIME_DIR + DBUS_SESSION_BUS_ADDRESS
    so we can call `systemctl --user` / `journalctl --user` from inside
    the camera-service process (which runs without a login session)."""
    import pwd
    try:
        uid = os.getuid()
    except AttributeError:
        uid = int(subprocess.check_output(["id", "-u"]).decode().strip())
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    return env


@app.get("/diag/network")
def diag_network():
    """Diagnose tunnel/network instability — why does cam.example.org keep
    dropping (HTTP 502 / CF error 1033)? Surfaces:
      - cloudflared service state + restart count + recent journal errors
      - WiFi link quality / signal (the #1 cause of tunnel flaps)
      - Pi undervoltage/throttle flags
      - load average + internet round-trip latency
    """
    uenv = _user_env()

    def _run(cmd, timeout=6):
        try:
            return subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                           timeout=timeout, env=uenv).decode(errors="replace").strip()
        except subprocess.CalledProcessError as e:
            return (e.output or b"").decode(errors="replace").strip()
        except Exception as exc:
            return f"error: {exc}"

    # cloudflared can be a system OR user service — try both.
    cf_state = _run(["systemctl", "is-active", "cloudflared"])
    if "inactive" in cf_state or "unknown" in cf_state or "error" in cf_state:
        cf_state_user = _run(["systemctl", "--user", "is-active", "cloudflared"])
        if cf_state_user not in ("", "unknown"):
            cf_state = f"user:{cf_state_user}"
    # NRestarts property reveals flapping
    cf_restarts = _run(["bash", "-c",
        "systemctl show cloudflared -p NRestarts 2>/dev/null || "
        "systemctl --user show cloudflared -p NRestarts 2>/dev/null || echo '?'"])
    cf_journal = _run(["bash", "-c",
        "journalctl -u cloudflared -n 20 --no-pager 2>/dev/null | grep -iE 'err|warn|fail|retry|connection|unregister' | tail -10 || echo '(no journal access)'"])

    # WiFi link quality — weak signal causes the tunnel to drop
    wifi = _run(["bash", "-c",
        "iw dev wlan0 link 2>/dev/null || iwconfig wlan0 2>/dev/null | grep -iE 'signal|quality|essid' || echo '(no wlan0)'"])

    # Undervoltage / throttle (0x0 = healthy; 0x50000 = under-volted now)
    throttled = _run(["bash", "-c", "vcgencmd get_throttled 2>/dev/null || echo 'n/a'"])

    # Load average + internet latency
    loadavg = _run(["cat", "/proc/loadavg"])
    ping = _run(["bash", "-c",
        "ping -c 3 -W 2 1.1.1.1 2>/dev/null | tail -2 || echo '(ping blocked)'"])

    # Default route / which interface carries traffic
    route = _run(["bash", "-c", "ip route get 1.1.1.1 2>/dev/null | head -1 || echo '?'"])

    hint = None
    if "error" in cf_state.lower() or "inactive" in cf_state.lower():
        hint = "cloudflared not active — tunnel down. Restart it (pi-deploy self-heals)."
    elif cf_restarts.isdigit() and int(cf_restarts.split("=")[-1] if "=" in cf_restarts else cf_restarts) > 5:
        hint = "cloudflared has restarted many times — flapping, likely WiFi instability."
    if "0x5" in throttled or "0x' " in throttled:
        hint = (hint or "") + " ⚠ Pi UNDERVOLTAGE — use a 5V/5A USB-C PSU; brown-outs reset WiFi+tunnel."

    return jsonify({
        "cloudflared_state":    cf_state,
        "cloudflared_restarts": cf_restarts,
        "cloudflared_errors":   cf_journal,
        "wifi_link":            wifi,
        "throttled":            throttled,
        "loadavg":              loadavg,
        "internet_ping":        ping,
        "default_route":        route,
        "hint":                 hint,
    })


@app.post("/diag/restart-tunnel")
def diag_restart_tunnel():
    """Restart cloudflared to recover a dropped tunnel."""
    uenv = _user_env()
    results = {}
    for scope in (["sudo", "-n", "systemctl"], ["systemctl", "--user"]):
        try:
            r = subprocess.run(scope + ["restart", "cloudflared"],
                               capture_output=True, timeout=15, env=uenv)
            results["+".join(scope)] = {"rc": r.returncode,
                                        "err": r.stderr.decode()[:120]}
            if r.returncode == 0:
                break
        except Exception as exc:
            results["+".join(scope)] = {"error": str(exc)[:120]}
    return jsonify({"ok": any(v.get("rc") == 0 for v in results.values()), "results": results})


@app.get("/diag/backend")
def diag_backend():
    """Show ivc-backend service state, last 80 journal lines, local health check,
    and last 40 lines of pi-deploy.log.  Used when example.org times out."""
    import pathlib

    uenv = _user_env()

    def _run(cmd, **kw):
        try:
            return subprocess.check_output(
                cmd, stderr=subprocess.STDOUT, timeout=6, env=uenv, **kw
            ).decode(errors="replace")
        except subprocess.CalledProcessError as e:
            return (e.output or b"").decode(errors="replace")
        except Exception as exc:
            return f"error: {exc}"

    state   = _run(["systemctl", "--user", "is-active", "ivc-backend"]).strip()
    journal = _run(["journalctl", "--user", "-u", "ivc-backend",
                    "-n", "80", "--no-pager", "--output=short-iso"])
    deploy  = _run(["tail", "-n", "50",
                    str(pathlib.Path.home() / "pi-deploy.log")])

    # Direct HTTP probe — is port 8000 actually answering?
    try:
        import urllib.request
        req  = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)
        health_resp = req.read().decode()
        health_code = req.getcode()
    except Exception as exc:
        health_resp = None
        health_code = f"error: {exc}"

    # Port listener check
    ports = _run(["ss", "-tlnp"])

    return jsonify({
        "state":        state,
        "journal":      journal,
        "deploy_log":   deploy,
        "health_check": {"code": health_code, "body": health_resp},
        "ss_ports":     ports,
    })


@app.post("/diag/restart-backend")
def diag_restart_backend():
    """Restart ivc-backend.  Tries systemctl --user first (with proper session env);
    falls back to killing the old uvicorn and launching a new one directly."""
    import pathlib

    uenv  = _user_env()
    home  = pathlib.Path.home()
    venv  = home / "ivc-venv" / "bin" / "uvicorn"
    bdir  = home / "ivc-backend"
    envf  = bdir / ".env"
    notes = []

    # 1. Try systemd (needs XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS set)
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "ivc-backend"],
            capture_output=True, timeout=18, env=uenv,
        )
        if r.returncode == 0:
            time.sleep(3)
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=4)
                return jsonify({"ok": True, "method": "systemd", "notes": notes})
            except Exception as probe_err:
                notes.append(f"systemd restart ok but health check failed: {probe_err}")
        else:
            notes.append(f"systemctl returned {r.returncode}: "
                         f"{r.stderr.decode(errors='replace')[:200]}")
    except Exception as exc:
        notes.append(f"systemctl failed: {exc}")

    # 2. Fallback — kill and re-launch uvicorn directly
    notes.append("falling back to direct launch")
    subprocess.run(["pkill", "-9", "-f", "uvicorn app.main"], capture_output=True)
    time.sleep(1.5)

    if not venv.exists():
        return jsonify({"ok": False, "notes": notes,
                        "error": f"uvicorn not found: {venv}"}), 500

    cmd = [str(venv), "app.main:app", "--host", "0.0.0.0",
           "--port", "8000", "--workers", "1"]
    if envf.exists():
        cmd += ["--env-file", str(envf)]

    proc = subprocess.Popen(
        cmd, cwd=str(bdir),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(4)

    try:
        import urllib.request
        body = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode()
        return jsonify({"ok": True, "method": "direct", "pid": proc.pid,
                        "health": body, "notes": notes})
    except Exception as probe_err:
        return jsonify({"ok": False, "method": "direct", "pid": proc.pid,
                        "health_error": str(probe_err), "notes": notes}), 500


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    # Browsers can't read custom response headers cross-origin unless we
    # explicitly expose them. We always expose X-Frame-Seq because the
    # long-polling client uses it to chain requests.
    existing = resp.headers.get("Access-Control-Expose-Headers", "")
    needed = "X-Frame-Seq"
    if needed not in existing:
        resp.headers["Access-Control-Expose-Headers"] = (
            f"{existing}, {needed}" if existing else needed
        )
    return resp


if __name__ == "__main__":
    print(f"Detected {len(_CAMERAS)} cameras:")
    for c in _CAMERAS.values():
        print(f"  {c.id}: {c.name}  ({c.device}, bus={c.bus})")
    # Re-apply the user's last fan choice before we start serving.  Without
    # this, every service restart (pi-deploy.sh kicks one every 60 s when
    # there's a backend change) drops the keep-alive thread and the fan
    # immediately ramps back up to max — annoying for the user.
    _fan_restore_on_boot()
    # Bind DUAL-STACK (IPv4 + IPv6). cloudflared's ingress targets
    # `http://localhost:8090`, and on this Pi `localhost` resolves to the IPv6
    # loopback `::1` — but we historically listened on IPv4 only, so cloudflared
    # got "dial tcp [::1]:8090: connect: connection refused" and the tunnel
    # returned 502 / CF-error-1033 intermittently. Binding to "::" on Debian
    # (net.ipv6.bindv6only=0 by default) accepts BOTH ::1 and 127.0.0.1, so
    # cloudflared AND the IPv4 backend proxy both reach us. Falls back to IPv4
    # if the IPv6 bind isn't available.
    try:
        app.run(host="::", port=8090, threaded=True)
    except OSError as e:
        print(f"[server] IPv6 dual-stack bind failed ({e}) — using IPv4 only", flush=True)
        app.run(host="0.0.0.0", port=8090, threaded=True)
