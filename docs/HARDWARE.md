# Hardware Technical Analysis — IVC Cage

> Integrated Intelligent Precision Metabolic & Behavioural IVC Cage
> Industry Project · Semester 4 · Client: the industry partner Lab

This document is the technical analysis for the physical device side of the
project. It covers the bill of materials, wiring, communication protocols,
firmware design, future-proofing decisions, and failure-mode analysis. It is
intended to be detailed enough that a third party could rebuild the device
from this document alone.

---

## 1. System architecture

```
                          ┌──────────────────────────────┐
                          │       Internet user           │
                          │  https://example.org (SPA)   │
                          │  https://cam.example.org     │
                          └──────────────┬───────────────┘
                                         │  HTTPS / WSS
                                         ▼
                         ┌──────────────────────────────┐
                         │   Cloudflare Tunnel edge      │
                         │ (no inbound ports on the Pi)  │
                         └──────────────┬───────────────┘
                                        │  encrypted outbound
                                        ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                  Raspberry Pi 5 (the "device")                │
   │  ──────────────────────────────────────────────────────────  │
   │  systemd user services (all auto-restart on failure):        │
   │   • cloudflared             — outbound tunnel                │
   │   • ivc-backend             — FastAPI · :8000                │
   │   • ivc-cameras             — V4L2 + servo + DHT11 proxy     │
   │   • ivc-arduino-bridge      — Mega serial → POST /ingest     │
   │   • ivc-env-ingester        — pushes DHT11 → backend          │
   │   • pi-deploy.timer         — 60s GitHub poll → redeploy     │
   │  ──────────────────────────────────────────────────────────  │
   │       USB 2.0                                  USB-V4L2       │
   │     ┌────────────────┐                  ┌─────────────────┐  │
   │     │  Arduino Mega  │                  │   USB cameras   │  │
   │     │     2560       │                  │   (1..N)        │  │
   │     └────────┬───────┘                  └─────────────────┘  │
   └──────────────┼─────────────────────────────────────────────────┘
                  │
   ┌──────────────┼──────────────────────────────────────────────────┐
   │              ▼                Cage hardware                      │
   │  ┌────────────────────────────────────────────────────────┐    │
   │  │  D2  — YF-S401 flow sensor (interrupt)                 │    │
   │  │  D3  — DHT11 (temperature + humidity, 1-wire)          │    │
   │  │  D4/D5,D6/D7,D8/D9 — 3× HX711 (FOOD/WATER/MOUSE)       │    │
   │  │  D11 — Food dispenser servo                            │    │
   │  │  D12 — Water pump relay (active-LOW)                   │    │
   │  │  D13 — Solenoid water-valve relay (active-LOW)         │    │
   │  │  D14/D15 — MH-Z19C CO2 (Serial3, optional)             │    │
   │  └────────────────────────────────────────────────────────┘    │
   │   O2 (JXW-02): NOT on the Mega → USB CH340 adapter on the Pi    │
   └──────────────────────────────────────────────────────────────────┘
```

---

## 2. Bill of materials (BOM)

| Component | Qty | Role | Notes / future-proofing |
|---|---:|---|---|
| Raspberry Pi 5 (8 GB) | 1 | Edge compute, web server, AI inference, deploy target | aarch64 kernel + armhf userland; Python 3.13; 32 GB SD-card OS image |
| Arduino Mega 2560 | 1 | Real-time GPIO bridge — load cells, flow, servo, relays, DHT11, CO2 | Chosen over Uno for the 54-pin headroom (3× HX711 + DHT11 + flow + 2 relays + servo + 2 hardware UARTs already in use); chosen over ESP32 because we want USB-serial reliability, not Wi-Fi |
| DHT11 (T/RH sensor) | 1 | Cage environment baseline | Single-wire protocol on **D3**, sampling cap 1 Hz, 2 s cache in firmware to avoid bus contention |
| HX711 + load cell | 3 | FOOD / WATER / MOUSE mass | 24-bit ADC, bit-banged DT/SCK pairs (D4/D5, D6/D7, D8/D9); per-cell scale + offset stored in EEPROM (magic `0x49564341`) |
| YF-S401 water flow sensor | 1 | Closed-loop volume dosing | Hall-effect pulse output on **D2** (interrupt, `INPUT_PULLUP`, RISING); ~5880 pulses/L |
| Water pump + relay | 1 | Push dosed water | Relay drive on **D12**, **active-LOW** (LOW = pump on) |
| Solenoid water valve + relay | 1 | Open/seal water line, kill siphon | Relay drive on **D13**, **active-LOW** (LOW = open / HIGH = closed); auto-closes when pump stops |
| Food dispenser servo (SG90) | 1 | Food gate | 4.8–6 V, PWM on **D11**. A second servo channel is `attach()`ed on **D10** in firmware but no servo is physically wired (phantom water-servo slot) |
| MH-Z19C CO2 sensor | 1 | CO2 (intended on Pi GPIO UART) | Firmware reserves **D14/D15** (Serial3 @ 9600); intended target is the Pi's `/dev/serial0`. Not yet wired/working |
| JXW-02 O2 sensor | 1 | O2 % (~20.9 % in air) | **Not on the Mega** — USB CH340 adapter on the Pi (auto-detected by VID `0x1A86`) |
| USB UVC webcams | 1–N | Behaviour streams | YUYV / MJPEG fallback; ffmpeg per-camera reader |
| 5 V/3 A PSU | 1 | Pi + servo + relays + pump | Servo/pump draw current peaks; PSU has 30 % headroom |
| LAN cable + Howest-IoT Wi-Fi | — | Redundant network paths | Pi has both wired + wireless; either alone is sufficient |
| Cloudflare account (free tier) | — | Public exposure | Replaces port-forwarding; no inbound ports on home/lab network |

---

## 3. Wiring

### 3.1 Arduino Mega pin map

This table is the authoritative pin map, verified against the firmware at
`device/arduino/ivc_sensors/ivc_sensors.ino`. Every `#define` value is unique —
there is **no GPIO conflict**: no two active devices share a pin.

| Mega pin | Dir | Signal (`#define`) | Component | Notes |
|---:|:---:|---|---|---|
| 5 V (rail) | — | — | Load cells, DHT11, servo, relays, sensors | PSU-backed, not USB-only |
| GND (rail) | — | — | Common ground | Star topology to PSU |
| **D2** | IN | `FLOW_PIN` | YF-S401 water flow sensor | Interrupt, `RISING`, `INPUT_PULLUP`; pulses counted in `flowPulseISR()` |
| **D3** | IN | `DHT_PIN` | DHT11 temp/humidity | 1-wire; internal pull-up sufficient at <20 cm cable |
| **D4** | IN | HX711 #1 DOUT/DT | Load cell **FOOD** (`scFood`) | `scFood.begin(4,5)` |
| **D5** | OUT | HX711 #1 PD_SCK/SCK | Load cell **FOOD** (`scFood`) | Bit-banged clock |
| **D6** | IN | HX711 #2 DOUT/DT | Load cell **WATER** (`scWater`) | `scWater.begin(6,7)` |
| **D7** | OUT | HX711 #2 PD_SCK/SCK | Load cell **WATER** (`scWater`) | Bit-banged clock |
| **D8** | IN | HX711 #3 DOUT/DT | Load cell **MOUSE** (`scMouse`) | `scMouse.begin(8,9)` |
| **D9** | OUT | HX711 #3 PD_SCK/SCK | Load cell **MOUSE** (`scMouse`) | Bit-banged clock |
| **D10** | OUT | `SERVO2_PIN` | *(unused water-servo slot)* | `servoWater.attach(10)` + `write(0)` run at boot and `servo/water` writes here, but **no servo is physically wired** — phantom channel, kept to avoid a pin clash |
| **D11** | OUT | `SERVO1_PIN` | Food dispenser servo (`servoFood`) | 50 Hz PWM, SG90 |
| **D12** | OUT | `RELAY_PUMP_PIN` | Water pump relay (RELAY1) | **active-LOW** — LOW = pump on |
| **D13** | OUT | `RELAY2_PIN` | Solenoid water-valve relay (RELAY2) | **active-LOW** — LOW = open / HIGH = closed |
| **D14** | OUT | TX3 | MH-Z19C CO2 | `Serial3.begin(9600)` — see §3.4 |
| **D15** | IN | RX3 | MH-Z19C CO2 | `Serial3.begin(9600)` — see §3.4 |
| **D16** | OUT | TX2 | JXW-02 O2 | `Serial2.begin(9600)` reserved (sensor is actually on the Pi — see §3.5) |
| **D17** | IN | RX2 | JXW-02 O2 | `Serial2.begin(9600)`, drained in `o2drain()` (sensor is actually on the Pi — see §3.5) |

> **Firmware-vs-comment caveats** (the firmware is the source of truth):
> - **D10 (water servo):** the source comment says "no servo wired", yet `setup()`
>   still calls `servoWater.attach(10)` / `write(0)` and `servo/water` commands write
>   to it. The pin is driven as a live PWM output even though nothing is connected.
> - **D14/D15 (CO2 Serial3):** commented "[optional/unused]", but the firmware **does**
>   init `Serial3`, disables ABC at boot, and polls `co2read()` every 5 s.
> - **D16/D17 (O2 Serial2):** commented as "USB on the Pi, NOT the Mega", yet the
>   firmware **does** init `Serial2` and parse it in `o2drain()`. In practice O2 is read
>   over USB on the Pi (§3.5); the Mega's UART2 reservation is harmless but inaccurate
>   to the comment.

### 3.2 Water system (pump + valve + flow)

The water subsystem performs **closed-loop, exact-volume dosing**:

- **Pump relay (D12)** and **solenoid valve relay (D13)** are both **active-LOW**.
- **`doseVolume(targetML)`** opens the valve, runs the pump, and counts YF-S401
  pulses (D2) to integrate dispensed millilitres. The relay is cut **early**
  (`targetML − DOSE_COAST_ML`) so coasting/residual flow lands on the exact target.
- A **safety timeout** (scaled to the target, clamped 10–120 s) prevents a runaway pump.
- **Siphon prevention:** when the pump stops (`pumpOff()`), the relay cuts the pump
  first, then `valveClose()` physically seals the line. The firmware then watches up
  to 10 s for residual ("siphon") flow and reports a `flow_reset` event.
- `{"pump":"on","dur":ms}` runs the pump for a fixed time; `{"dose":mL}` does the
  metered dose (1–1000 mL).

### 3.3 Mass sensing (3× HX711)

Three HX711 24-bit load-cell amplifiers share no pins (DT/SCK pairs on
D4/D5, D6/D7, D8/D9) and map to **FOOD**, **WATER**, and **MOUSE**. Calibration
(scale factor + tare offset per cell) is held in EEPROM behind magic word
`0x49564341`; `c` calibrates and `t` tares over USB serial.

### 3.4 CO2 — MH-Z19C

Intended to run on the **Raspberry Pi's GPIO UART `/dev/serial0`** (GPIO14/15,
header pins 8/10); the Pi's serial login console and Bluetooth were disabled to
free that UART. The Mega firmware also reserves **Serial3 on D14/D15** and polls
the sensor every 5 s, but CO2 is **not yet wired/working** end-to-end.

### 3.5 O2 — JXW-02

The O2 sensor connects to the **Raspberry Pi over a USB CH340 adapter**, *not* the
Arduino. The Pi bridge auto-detects it by USB vendor ID `0x1A86` (QinHeng CH340),
so it survives `ttyUSB0`/`ttyUSB1` renumbering across reboots. It reads ~20.9 % O2
in room air. **Known issue:** a loose wire on the USB adapter board currently needs
reseating.

### 3.6 Pi ↔ Arduino

USB 2.0 (cable provides both power and data) → `/dev/ttyACM0` (or `ttyUSB0` depending on bootloader). Baud rate 115 200, 8-N-1, no flow control. Hot-pluggable; the camera-stream service auto-reconnects on disconnect.

### 3.7 Pi ↔ Cameras

USB 2.0 hub (powered, if more than two cameras) → V4L2 device nodes `/dev/video0`, `/dev/video2`, … (every other index because each UVC camera exposes both a video and metadata node).

---

## 4. Communication protocols

### 4.1 DHT11 (Arduino-side, no library)

The firmware implements the DHT11 single-wire timing protocol inline rather than depending on a library, for two reasons: (a) deterministic timing under interrupt load, and (b) a smaller binary (8 250 bytes ≈ 3 % of Mega flash, leaving room for future sensors).

```
Master  ___          _______________________________________
           |________|         (18 ms start-LOW)
              start
DHT11   _____________     ____         ____    ____
                       |_|    |_______|    |__|    |_  ... 40 bits ...
                        80 µs ACK     50 µs per bit prelude
                                       26–28 µs → 0
                                       70 µs    → 1
```

**Frame layout** (40 bits, MSB-first):
```
  byte 0     byte 1     byte 2     byte 3     byte 4
  RH int     RH dec     T int      T dec      checksum
```

Checksum = `(byte0 + byte1 + byte2 + byte3) & 0xFF`. We re-read when the
checksum fails, capping at 3 retries; on permanent failure the firmware
returns the last good cached value with an `env_age` field so the consumer
can drop stale data.

### 4.2 Arduino ↔ Pi (USB-serial JSON protocol)

We use newline-terminated JSON instead of a binary protocol to keep the
firmware debuggable from the Pi with nothing but `cat /dev/ttyACM0`.

Command lines from the Pi (each newline-terminated, parsed on the leading `{`):
```json
{"dose":100}                 // dispense exactly 100 mL via flow-sensor feedback (1–1000 mL)
{"pump":"on","dur":5000}     // run pump for a fixed time (ms, clamped 100–30000)
{"pump":"off"}               // stop pump + close valve (siphon kill)
{"servo":"food","angle":90}  // food servo 0–180°
```
Single-character serial keys also work: `c` = calibrate load cells,
`t` = tare, `z` = CO2 zero.

The Mega streams telemetry as newline JSON the bridge ingests, including the
load-cell masses, DHT11 temp/humidity, CO2, O2, and flow/dose events such as:
```json
{"event":"flow_reset","action":"pump_stopped"}
{"status":"ok","dose":100.0}
```

### 4.3 Camera-stream HTTP API (Pi-internal)

```
GET  /streams                            → list of available cameras
GET  /stream/{idx}                       → multipart MJPEG stream
GET  /snapshot/{idx}                     → one JPEG frame
GET  /sensor/env                         → {connected, temperature_c, humidity_pct, age_s, ts}
POST /servo/{name}/open|close|pulse      → forwards to Arduino over serial
GET  /health                             → liveness + Arduino + camera count
```

The `/sensor/env` endpoint is the bridge that the **env-ingester** polls
every 30 s and republishes to the backend's `/api/v1/ingest` with the cage
ID and a JWT bearer token.

**Arduino bridge (systemd `ivc-arduino-bridge`).** The Mega's USB-serial
telemetry stream is read by a dedicated Pi service, `device/arduino-bridge/bridge.py`
(run as the systemd unit `ivc-arduino-bridge`). It parses the per-line JSON from
the Mega, merges in the O2 reading from the separate USB sensor (`/dev/ttyUSB*`,
CH340 VID `0x1A86`), and **POSTs the combined reading to
`https://example.org/api/v1/ingest`** (`BACKEND_URL` is configurable; default
`https://example.org`). Relay/valve/servo commands are queued back to the Mega
over the same serial link.

### 4.4 Backend ↔ Frontend

REST over HTTPS at `/api/v1/*`, WebSocket at `/ws` for push updates
(behaviour labels, alerts, readings). JWT bearer tokens (12 h TTL, bcrypt
password hashing, rate-limited login endpoint).

### 4.5 Cloudflare Tunnel (no inbound ports)

The Pi runs `cloudflared` which dials *outbound* to Cloudflare's edge over
QUIC. Cloudflare then receives `https://example.org` / `https://cam.example.org`
on its anycast IPs and proxies the connection back through the established
tunnel. **The Pi has zero open inbound ports on the LAN/Wi-Fi.**

This is the "previously unseen protocol" requirement — students typically
expose a Pi with port-forwarding (insecure, ISP-dependent) or with a VPN
(adds client friction). Cloudflare Tunnel removes both.

---

## 5. Firmware design

```cpp
// ivc_sensors.ino — overview
setup() {
    Serial.begin(115200);            // USB telemetry/commands
    Serial2.begin(9600);             // O2 UART2 (D16/D17) — see §3.5
    Serial3.begin(9600);             // CO2 UART3 (D14/D15) — see §3.4
    dht.begin();                     // DHT11 on D3
    pinMode(RELAY_PUMP_PIN, OUTPUT); digitalWrite(RELAY_PUMP_PIN, HIGH); // active-LOW: off
    pinMode(RELAY2_PIN,    OUTPUT); digitalWrite(RELAY2_PIN,    HIGH);   // valve closed
    pinMode(FLOW_PIN, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(FLOW_PIN), flowPulseISR, RISING);
    servoFood.attach(SERVO1_PIN);    // D11
    scFood.begin(4,5); scWater.begin(6,7); scMouse.begin(8,9); // 3× HX711
    loadCalib();                     // scale + tare from EEPROM
    co2cmd(0x79, 0x00);              // disable CO2 ABC at boot
}

loop() {
    o2drain();                       // pull any O2 UART bytes
    if (Serial.available()) handleLine();  // dose / pump / servo / c / t / z
    // dose state machine: count flow pulses, early-cut relay, siphon watch
    // periodic CO2 read every 5 s; telemetry JSON emitted on USB serial
}
```

**Why a 2 s cache rather than read-on-demand?** Two reasons:
1. DHT11's spec limits sampling to ≤ 1 Hz; a hot poll would return stale
   or error data.
2. Every Arduino response includes the temperature/humidity values, so by
   caching we make the sensor read **free** from the Pi's point of view.

---

## 6. Power & safety analysis

| Risk | Mitigation |
|---|---|
| Servo stall current spike | Dedicated 5 V/3 A PSU with 30 % headroom; bulk capacitor across servo rail |
| USB cable disconnect during streaming | `CameraCapture` reader thread auto-reopens; ffmpeg restart with backoff |
| Pi power loss mid-write | SQLite WAL mode + `checkpoint_wal()` on boot to clear stale locks |
| DHT11 wire short to 5 V | Mega's GPIO is 5 V-tolerant — no level-shifter needed; **input pin clamped via internal protection diode** |
| Servo over-rotation | Firmware clamps to 0–180 ° and refuses commands outside that range |
| Arduino reset on USB enumeration | Camera-stream service waits 2 s after `/dev/ttyACM0` reappears before re-sending commands |

---

## 7. Future-proofing decisions

| Decision | Rationale |
|---|---|
| **Arduino Mega over Uno/Nano** | 54 GPIO pins → can grow to 8+ sensors without a board swap (Uno has only 14 digital + 6 analog) |
| **JSON line protocol over binary** | Trivial to add new commands without changing parsing; can be debugged from the Pi with `cat` |
| **Pi 5 over Pi Zero** | 8 GB RAM accommodates ML inference + ffmpeg + browser preview with significant headroom; aarch64 kernel future-proof to ARMv9 |
| **SQLite WAL over Postgres** | One process, no network — perfect for an edge device; readings table indexed on (cage, sensor, ts) handles 10⁵+ rows per cage per day |
| **Cloudflare Tunnel over port-forwarding** | Survives ISP IP changes, no router config, ports closed by default |
| **systemd user services over Docker** | Sub-second boot, native journal access, no daemon-in-daemon overhead on the Pi |
| **GitHub-poll auto-deploy over CI push** | No webhook public endpoint needed; Pi pulls outbound only; pipeline tolerates ISP outages (catches up on the next tick) |
| **`--delete` rsync on frontend only** | Backend code is `--delete`-d (clean state); `data/` is excluded so the SQLite file never gets nuked by deploys |
| **Two AI models (classifier + anomaly detector)** | Discriminative classifier puts every window in a bucket; anomaly detector flags windows that don't fit *any* bucket — together they catch both expected behaviour drift and unexpected metabolic events |

---

## 8. Data flow — single reading lifecycle

```
DHT11 wire             camera-stream         env-ingester       FastAPI backend
─────────────────────────────────────────────────────────────────────────────
  T,RH sample ─┐
               │ 2 s cache
               ▼
  Arduino JSON ─────serial──> /sensor/env ─┐
                                            │ 30 s poll
                                            ▼
                                       JWT POST ──────────> /api/v1/ingest
                                                                  │
                                                                  ▼
                                                            INSERT readings
                                                                  │
                                                                  ▼
                                                         evaluate_rules()
                                                                  │
                                                                  ▼
                                                         anomaly.observe()
                                                                  │
                                                                  ▼
                                                       broadcast WS event
                                                                  │
                                                                  ▼
                                                        React dashboard
                                                          updates live
```

Latency budget (measured end-to-end): wire → dashboard ≈ **800 ms p95**
(of which 30 s is the env-ingester poll interval; the WS publish is < 50 ms).

---

## 9. Security analysis (device perspective)

| Surface | Control |
|---|---|
| Public HTTPS | Cloudflare-terminated TLS (≥ 1.2), HSTS 1 year, CSP, JWT auth |
| LAN HTTP (192.168.50.2:8000) | Same JWT auth; only accessible from same subnet |
| USB-serial | Local-only, no auth needed (physical access required to attach the Mega) |
| systemd user services | Run as `grazwis`, no root daemons, polkit rules narrow sudo to specific binaries |
| SSH | Key-only auth (`~/.ssh/id_ed25519_pi`), password disabled |
| Auto-deploy | Pull-only — Pi never accepts pushes; supply-chain risk is bounded to commits visible on GitHub `main` |
| Camera streams | `cam.example.org` requires either same JWT or LAN access (currently anonymous on LAN by design — researcher-only network) |

---

## 10. Failure modes and recovery

| Failure | Symptom | Auto-recovery |
|---|---|---|
| Arduino disconnect | `/sensor/env` returns `connected:false` | Camera-stream waits for `/dev/ttyACM*` to reappear, reopens automatically |
| Camera disconnect | Stream stalls, `/snapshot` 503 | `CameraCapture` restarts the ffmpeg subprocess on EOF, with exponential backoff |
| Backend OOM / crash | 502 from Cloudflare | `systemd --user` restarts within 1 s (`Restart=on-failure`) |
| Cloudflare tunnel drop | example.org unreachable | `cloudflared` re-establishes within 5 s; outbound-only so no firewall to negotiate |
| Pi loses network | Auto-deploys queue | `Persistent=true` on the timer fires the missed `OnUnitInactiveSec` once connectivity returns |
| Bad commit on `main` | Service may fail to start | Last-good binary stays in `~/ivc-backend/`; only changed subsystems restart, so unrelated services keep running |

---

## 11. Reproducibility

To rebuild this device end-to-end on a fresh Pi 5:

```bash
# 1. Flash Raspberry Pi OS 64-bit, enable SSH, set user "grazwis"
# 2. SSH in and clone the repo
git clone https://github.com/MustafazadaAghasalim/industryprojectfinal.git ~/IndustryProject

# 3. Bootstrap once (installs venv, systemd units, cloudflared)
~/IndustryProject/scripts/pi-bootstrap.sh   # see scripts/

# 4. Wire the Arduino Mega per § 3.1, plug in USB
# 5. Flash the firmware
sudo avrdude -v -patmega2560 -cwiring -P/dev/ttyACM0 -b115200 -D \
     -Uflash:w:/home/grazwis/ivc_sensors.hex:i

# 6. Enable auto-deploy + start services
systemctl --user enable --now ivc-backend ivc-cameras ivc-env-ingester pi-deploy.timer

# 7. Authenticate cloudflared and point example.org at the tunnel ID
sudo systemctl enable --now cloudflared

# That's it — every future change ships via `git push origin main`.
```

---

## 12. Performance envelope (measured on Pi 5)

| Metric | Value |
|---|---|
| Cold-boot to dashboard live | **≈ 22 s** |
| Auto-deploy cycle (frontend rebuild) | **≈ 50–80 s** |
| `/api/v1/ingest` p95 latency | **< 40 ms** |
| WebSocket fan-out per event | **< 5 ms** |
| MJPEG stream throughput | **15–25 fps @ 640×480** per camera |
| Concurrent dashboard sessions | **tested at 5, no degradation** |
| SQLite WAL writes / second | **300+ sustained** |
| Anomaly detector CPU per cage per tick | **< 1 ms** (240-sample sliding window) |

---

*Last updated: 2026-06-10 · pin map verified against
`device/arduino/ivc_sensors/ivc_sensors.ino` · maintained alongside source code at
`docs/HARDWARE.md`.*
