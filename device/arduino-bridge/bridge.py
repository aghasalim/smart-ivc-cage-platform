#!/usr/bin/env python3
"""
IVC Cage — Arduino serial bridge.

Reads JSON sensor lines from the Arduino Mega on /dev/ttyACM0 (115200 baud)
and POSTs each reading to the backend ingest endpoint every 5 s.

Config via .env  (copy env.example → .env and fill in):
  API_KEY      ivc_... key from Settings → API Keys
  BACKEND_URL  https://example.org
  CAGE_ID      cage id (e.g. CAGE-01)
  SERIAL_PORT  /dev/ttyACM0
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import serial
import serial.tools.list_ports

# ── Config ────────────────────────────────────────────────────────
_ENV_FILE = Path(__file__).parent / ".env"

def _load_env():
    cfg: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg

_cfg         = _load_env()
API_KEY      = _cfg.get("API_KEY",      os.environ.get("API_KEY",      ""))
BACKEND_URL  = _cfg.get("BACKEND_URL",  os.environ.get("BACKEND_URL",  "https://example.org"))
CAGE_ID      = _cfg.get("CAGE_ID",      os.environ.get("CAGE_ID",      "CAGE-01"))
SERIAL_PORT  = _cfg.get("SERIAL_PORT",  os.environ.get("SERIAL_PORT",  "/dev/ttyACM0"))
BAUD         = int(_cfg.get("BAUD",     os.environ.get("BAUD",         "115200")))
POST_URL     = f"{BACKEND_URL.rstrip('/')}/api/v1/ingest"

# ── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bridge] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path.home() / "arduino-bridge" / "bridge.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── State for delta calculations ──────────────────────────────────
_prev_food_g:  float | None = None
_prev_water_g: float | None = None
# Start seq from current unix timestamp so restarts never collide with prior runs.
import time as _time
_seq: int = int(_time.time())

# ── O2 sensor on its own USB-serial port (CH340, auto-detected) ──────
# The JXW-02 streams a 10-byte frame at 9600 baud:
#   FF 01 07 01 <hi> <lo> 00 00 00 <csum>   →  O2% = (hi<<8 | lo) / 10
# The CH340 adapter (VID 0x1A86) can enumerate as ttyUSB0/1/... across reboots,
# so we auto-detect it by USB vendor ID rather than a fixed device path.
O2_PORT = _cfg.get("O2_PORT", os.environ.get("O2_PORT", ""))  # "" = auto-detect
O2_BAUD = int(_cfg.get("O2_BAUD", os.environ.get("O2_BAUD", "9600")))
O2_VID  = 0x1A86  # QinHeng CH340
O2_STALE_MS = 15000          # if no valid frame for this long, treat O2 as unavailable
_last_o2: float | None = None
_last_o2_ms: float = 0.0     # monotonic ms of last accepted frame
_o2_buf = bytearray()


def find_o2_port() -> str | None:
    """Return the O2 CH340 serial port. Honour O2_PORT if set & present,
    else scan for a CH340 (VID 0x1A86), excluding the Arduino's own port
    (some Arduino clones are also CH340)."""
    if O2_PORT and Path(O2_PORT).exists():
        return O2_PORT
    arduino = SERIAL_PORT if Path(SERIAL_PORT).exists() else None
    for p in serial.tools.list_ports.comports():
        if p.vid == O2_VID and p.device != arduino:
            return p.device
    return None


def _o2_checksum_ok(frame: bytes) -> bool:
    """JXW-02 / Winsen-style checksum: (~sum(bytes[1:8]) + 1) & 0xFF == byte[8]
    for a 9-byte frame, or validate the 10-byte active-upload frame's trailing
    byte. We accept either common layout to stay robust across firmware revs."""
    s9 = (~(sum(frame[1:8]) & 0xFF) + 1) & 0xFF
    if s9 == frame[8]:
        return True
    s10 = (~(sum(frame[1:9]) & 0xFF) + 1) & 0xFF
    return s10 == frame[9]


def read_o2(o2ser) -> None:
    """Non-blocking: drain available bytes, parse validated frames → _last_o2.
    Only accepts a frame with the documented header (FF 01 07 01) AND a valid
    checksum, so line noise / wrong-baud garbage can't inject a bogus reading.
    On a header mismatch we advance by ONE byte to re-sync (not 10)."""
    global _last_o2, _last_o2_ms, _o2_buf
    if o2ser is None:
        return
    try:
        n = o2ser.in_waiting
        if n:
            _o2_buf.extend(o2ser.read(n))
    except Exception:
        # Port hiccup — let the caller's reconnect logic handle reopening.
        raise
    if len(_o2_buf) > 200:
        _o2_buf = _o2_buf[-100:]
    i = 0
    while i + 10 <= len(_o2_buf):
        frame = _o2_buf[i:i + 10]
        # Require the documented header before trusting the frame.
        if not (frame[0] == 0xFF and frame[1] == 0x01 and frame[2] == 0x07 and frame[3] == 0x01):
            i += 1
            continue
        if not _o2_checksum_ok(frame):
            i += 1
            continue
        val = ((frame[4] << 8) | frame[5]) / 10.0
        if 0.0 < val <= 25.0:
            _last_o2 = round(val, 1)
            _last_o2_ms = time.monotonic() * 1000.0
        i += 10
    _o2_buf = _o2_buf[i:]


def o2_value() -> float | None:
    """Current O2 reading, or None if stale (sensor unplugged / silent)."""
    if _last_o2 is None:
        return None
    if (time.monotonic() * 1000.0) - _last_o2_ms > O2_STALE_MS:
        return None
    return _last_o2


def find_arduino_port() -> str:
    """Return SERIAL_PORT if it exists, otherwise scan for an ACM/USB device."""
    if Path(SERIAL_PORT).exists():
        return SERIAL_PORT
    for p in serial.tools.list_ports.comports():
        if "ACM" in p.device or "USB" in p.device:
            log.info(f"Auto-detected Arduino on {p.device}")
            return p.device
    return SERIAL_PORT   # fall back; open() will raise a clear error


def to_payload(data: dict) -> dict | None:
    """Map raw Arduino JSON → backend IngestIn schema."""
    global _prev_food_g, _prev_water_g, _seq
    # Use wall-clock seconds as seq so restarts/overlapping runs never collide
    # on the (cage_id, sensor, seq) unique constraint. Readings are ~5s apart,
    # so per-second resolution is always unique; monotonic guard avoids a tie.
    now_s = int(_time.time())
    _seq = now_s if now_s > _seq else _seq + 1

    sensors: dict = {}

    # ── Mouse body weight (accept any value — negative = pre-tare) ──
    mouse_g = data.get("mouse_g")
    if isinstance(mouse_g, (int, float)) and mouse_g > -9000:
        sensors["weighing"] = {"animal_g": round(abs(float(mouse_g)), 2)}

    # ── Food hopper ───────────────────────────────────────────────
    food_g = data.get("food_g")
    if isinstance(food_g, (int, float)) and food_g > -9000:
        food_g = abs(float(food_g))
        delivered = 0.0
        if _prev_food_g is not None and _prev_food_g > food_g + 0.5:
            delivered = round(_prev_food_g - food_g, 2)
        sensors["feeding"] = {
            "delivered_g":  delivered,
            "remaining_g":  round(food_g, 2),
            "wasted_g":     0.0,
            "gate_state":   "open",
        }
        _prev_food_g = food_g

    # ── Water bottle ──────────────────────────────────────────────
    water_g = data.get("water_g")
    if isinstance(water_g, (int, float)) and water_g > -9000:
        water_g = abs(float(water_g))
        flow_ml = 0.0
        if _prev_water_g is not None and _prev_water_g > water_g + 0.1:
            flow_ml = round(_prev_water_g - water_g, 2)
        sensors["water"] = {
            "flow_ml":  flow_ml,
            "tank_ml":  round(water_g, 2),
        }
        _prev_water_g = water_g

    # ── Environment: DHT11 + CO2 + O2 ────────────────────────────
    co2    = data.get("co2_ppm", -1)
    co2_ok = data.get("co2_ok", False)
    # O2 comes from the dedicated USB sensor (auto-detected CH340), not the
    # Arduino. o2_value() returns None if the reading is stale (unplugged/silent),
    # so we fall back to the Arduino's o2_pct rather than publishing a frozen value.
    _o2 = o2_value()
    o2  = _o2 if _o2 is not None else data.get("o2_pct", -1)
    temp   = data.get("temp_c")
    hum    = data.get("hum_pct")
    env: dict = {}
    if isinstance(temp, (int, float)) and temp > -100:
        env["temperature_c"] = round(float(temp), 1)
    if isinstance(hum, (int, float)) and hum >= 0:
        env["humidity_pct"] = round(float(hum), 1)
    if co2_ok and isinstance(co2, int) and co2 > 0:
        env["co2_ppm"] = co2
    if isinstance(o2, (int, float)) and float(o2) > 0:
        env["o2_pct"] = round(float(o2), 2)
    if env:
        sensors["environment"] = env

    if not sensors:
        return None

    return {
        "cage_id": CAGE_ID,
        "ts":      datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "seq":     _seq,
        "sensors": sensors,
    }


def post(payload: dict) -> bool:
    """Blocking POST with a generous timeout. Called only from the poster thread,
    so a slow backend never stalls the serial read / command-relay loop."""
    try:
        r = requests.post(
            POST_URL,
            json=payload,
            headers={"X-API-Key":    API_KEY,
                     "Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code in (200, 202):
            log.info(f"→ backend  stored={r.json().get('stored',0)}")
            return True
        log.warning(f"→ backend HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as exc:
        log.warning(f"→ backend error: {exc}")
        return False


# ── Background backend poster ─────────────────────────────────────
# The main loop must stay responsive (read sensors, relay servo/pump
# commands every ~2s). Network POSTs can take seconds, so they run on a
# separate thread fed by a bounded queue. If the queue fills (backend down),
# the oldest payload is dropped so we never accumulate unbounded memory.
_post_q: "queue.Queue[dict]" = queue.Queue(maxsize=20)


def _poster_worker():
    while True:
        payload = _post_q.get()
        try:
            post(payload)
        except Exception as exc:
            log.warning(f"poster thread error: {exc}")
        finally:
            _post_q.task_done()


def enqueue_post(payload: dict) -> None:
    """Non-blocking: hand the payload to the poster thread. Drop oldest if full."""
    try:
        _post_q.put_nowait(payload)
    except queue.Full:
        try:
            _post_q.get_nowait()       # drop oldest
            _post_q.task_done()
        except queue.Empty:
            pass
        try:
            _post_q.put_nowait(payload)
        except queue.Full:
            log.warning("post queue full — dropping reading")


def run() -> None:
    if not API_KEY or not API_KEY.startswith("ivc_"):
        log.error("API_KEY missing or invalid. "
                  f"Copy {_ENV_FILE.parent}/env.example → .env and set API_KEY=ivc_...")
        sys.exit(1)

    log.info(f"IVC Arduino bridge starting  port={SERIAL_PORT}  cage={CAGE_ID}  backend={BACKEND_URL}")

    # Start the background backend poster (daemon → dies with the process).
    threading.Thread(target=_poster_worker, daemon=True, name="poster").start()

    CMD_FILE = "/tmp/ivc-arduino-cmd"

    def _check_cmd_file(ser):
        """If the camera service wrote a command to the queue file, relay it
        to the Arduino and delete the file. Non-blocking."""
        try:
            if os.path.exists(CMD_FILE):
                with open(CMD_FILE) as f:
                    cmd = f.read().strip()
                os.remove(CMD_FILE)
                if cmd:
                    ser.write((cmd + "\n").encode())
                    log.info(f"Relayed cmd → Arduino: {cmd[:80]}")
        except Exception:
            pass

    # O2 USB sensor — opened lazily and re-opened automatically if it drops
    # (USB renumber / unplug). open_o2() returns a Serial or None.
    o2_state = {"ser": None, "next_try_ms": 0.0}

    def open_o2():
        now = time.monotonic() * 1000.0
        if o2_state["ser"] is not None or now < o2_state["next_try_ms"]:
            return
        o2_state["next_try_ms"] = now + 5000.0  # retry at most every 5s
        port = find_o2_port()
        if not port:
            return
        try:
            o2_state["ser"] = serial.Serial(port, O2_BAUD, timeout=0)
            log.info(f"Connected to O2 sensor on {port} @ {O2_BAUD}")
        except Exception as exc:
            log.warning(f"O2 sensor at {port} not opened: {exc}")

    def poll_o2():
        open_o2()
        if o2_state["ser"] is None:
            return
        try:
            read_o2(o2_state["ser"])
        except Exception as exc:
            # Port died (unplug/renumber) — close and let open_o2 re-detect it.
            log.warning(f"O2 read failed ({exc}) — will re-detect")
            try:
                o2_state["ser"].close()
            except Exception:
                pass
            o2_state["ser"] = None

    open_o2()

    while True:
        port = find_arduino_port()
        try:
            # Disable DTR hang-up at the OS level before opening so the
            # Arduino does NOT reset when pyserial opens the port.
            import subprocess
            subprocess.run(["stty", "-F", port, "-hupcl"], check=False)
            with serial.Serial(port, BAUD, timeout=2,
                               dsrdtr=False, rtscts=False,
                               exclusive=True) as ser:
                ser.dtr = False
                log.info(f"Connected to Arduino on {port}")
                time.sleep(0.5)  # let any boot noise drain
                ser.reset_input_buffer()
                while True:
                    # Check for commands from the camera service
                    _check_cmd_file(ser)
                    # Poll the O2 USB sensor (non-blocking; auto-reconnects)
                    poll_o2()
                    # Read a line (timeout=2s so we cycle back to check cmds)
                    raw_line = ser.readline()
                    if not raw_line:
                        continue
                    try:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue

                    if not line.startswith("{"):
                        log.debug(f"Arduino: {line}")
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning(f"Bad JSON: {line[:80]}")
                        continue

                    # Skip event/info messages
                    if "event" in data:
                        log.info(f"Arduino event: {data.get('msg', data)}")
                        continue

                    log.info(f"Sensors: food={data.get('food_g')}g  "
                             f"water={data.get('water_g')}g  "
                             f"mouse={data.get('mouse_g')}g  "
                             f"flow={data.get('flow_ml')}mL  "
                             f"pump={data.get('pump_on')}  "
                             f"CO2={data.get('co2_ppm')}ppm  "
                             f"O2={o2_value() if o2_value() is not None else data.get('o2_pct')}%  "
                             f"temp={data.get('temp_c')}C  "
                             f"hum={data.get('hum_pct')}%")

                    payload = to_payload(data)
                    if payload:
                        enqueue_post(payload)  # non-blocking — poster thread handles it

                    # Push DHT values to camera service so /sensor/env stays fresh
                    temp = data.get("temp_c")
                    hum = data.get("hum_pct")
                    if (isinstance(temp, (int, float)) and temp > -100) or \
                       (isinstance(hum, (int, float)) and hum >= 0):
                        try:
                            # localhost — keep timeout tiny so a hung camera
                            # service can't stall the control loop.
                            requests.post(
                                "http://127.0.0.1:8090/sensor/env/update",
                                json={"temperature_c": temp, "humidity_pct": hum},
                                timeout=0.3,
                            )
                        except Exception:
                            pass

        except serial.SerialException as exc:
            log.warning(f"Serial error ({exc}) — retrying in 5 s")
            time.sleep(5)
        except Exception as exc:
            log.error(f"Unexpected error: {exc} — retrying in 5 s")
            time.sleep(5)


if __name__ == "__main__":
    run()
