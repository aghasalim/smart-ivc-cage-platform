# Getting Started — IVC Cage Platform

> **Integrated Intelligent Precision Metabolic & Behavioural IVC Cage**
> A full hardware + software stack for a next-generation Individually Ventilated
> Cage (IVC) for laboratory mice: precision feeding, closed-loop water dosing,
> behavioural monitoring (multi-camera + AI), environmental and metabolic gas
> sensing, all driven from a multilingual web dashboard.

This document answers two questions:

1. **How does a user get started with the application?** — the user-facing
   walkthrough of every feature and action.
2. **How do I install and run it from the submitted source on a fresh PC?** —
   a step-by-step "Getting Started" reproduction guide.

---

## Part 1 — How a user gets started (functionality walkthrough)

The platform is operated entirely through a **web dashboard** — no command line is
needed to use it. A researcher can monitor and control the cage from any device
with a browser.

### 1.1 Reaching the application

| Method | URL |
|---|---|
| **Live production instance** | <https://example.org> (cameras: <https://cam.example.org>) |
| **Self-hosted (local)** | `http://localhost:5173` after running it yourself (Part 2) |

The dashboard is an installable **PWA** (Progressive Web App) — on a phone or
tablet you can "Add to Home Screen" and it behaves like a native app.

### 1.2 Logging in

The app opens on a **login screen**. Sign in with a seeded account:

| Username | Password | Role | Can do |
|---|---|---|---|
| `researcher` | `change-me-please` | researcher | View + operate everything |
| `admin` | `change-me-please` | admin | Everything + admin-only endpoints |

- Authentication uses **JWT bearer tokens** (12-hour expiry) with **bcrypt**
  password hashing and **constant-time** verification; the login endpoint is
  **rate-limited** to resist brute-force.
- After login, the token is stored client-side and used for all API and
  WebSocket calls. Expired sessions return you to the login screen.

> ⚠️ The demo credentials are pre-filled **only in development builds**. In
> production the fields are empty and no hint is shown.

### 1.3 Choosing a language

A **language switcher** is available on every page:
**🇬🇧 English · 🇳🇱 Nederlands · 🇫🇷 Français · 🇨🇳 中文.**
The choice is saved in the browser. All UI strings are fully translated
(key-symmetric across all four locales).

### 1.4 The dashboard at a glance

A left **sidebar** gives eight sections. Everything is **real-time**: sensor
values, charts, alerts, and camera/actuator state stream in over a **WebSocket**,
so the UI updates live without manual refreshes. A header badge shows the live
cage temperature/humidity on every page.

---

### 1.5 The eight sections — what the user can do

#### 🏠 Overview
The landing page. All cages at a glance, each as a card showing live KPIs
(temperature, humidity, O₂, CO₂, animal weight, water/food levels, activity), an
**online/offline** indicator (from each cage's "last seen"), and the number of
open alerts. **Clicking a cage** opens its detailed page. If the API is
unreachable, a clear error banner is shown (distinct from "no cages yet").

#### 🐭 Cage detail
Everything about one cage:
- **Live sensor readings** and per-metric **charts**.
- An **interactive time-series explorer**: choose a metric, time granularity
  (hour/day), aggregation (avg/sum/min/max/last), and window.
- **Data export** in five formats — **CSV, JSON, XLSX, PDF, DOCX** — for
  statistical analysis (R, SPSS, Excel). Exports are streamed and row-capped so
  large ranges don't hang.
- Loading / empty / error states are handled explicitly, and a bad cage id
  produces a proper "not found" rather than a blank page.

#### 📷 Cameras
- **Live MJPEG video** from each USB camera, with **full-screen** view and
  one-click **snapshot download** (JPEG).
- A **Live AI Detection** panel overlays the YOLO mouse detection/tracking
  output (per-camera mouse count, "no mice in frame", last-seen age).
- **Per-camera settings panel (⚙ gear)** — **18+ adjustable functions**:
  - *Image:* brightness, contrast, saturation, hue, gamma, sharpness
  - *Exposure:* auto/manual mode, exposure time, gain, dynamic-FPS, backlight compensation
  - *Colour:* white-balance auto, white-balance temperature
  - *Focus:* auto-focus, manual focus distance
  - *Other:* anti-flicker (50/60 Hz)
  - *Digital zoom:* 1–4× zoom with X/Y pan (client-side, since these cameras
    have no optical zoom)
  - *Reset to defaults* (restores the IR-optimised baseline, not the washed-out
    factory exposure)
  - Controls apply to the **live feed instantly** (no stream restart) and are
    validated server-side.

#### 🔔 Alerts
- **Rule-based alerts** (e.g. RER out of physiological range, low food/water
  intake, no activity) **and** alerts from the streaming **AI anomaly detector**
  (per-cage online baseline that flags drift before it crosses a hard threshold).
- The user can **acknowledge** alerts; a failed acknowledge surfaces an error
  toast so nothing is silently lost.

#### 📅 Schedule
Three control groups, all backed by real hardware actions:
- **Feeding window editor** — set start/end time (timezone-aware, with a live
  "opens in X min" countdown), the days of the week, and an active toggle. A
  backend **scheduler actually executes this**: it opens the food-gate servo at
  the start time and closes it at the end time, every day in the configured
  window.
- **Manual feed/valve control** — open, close, or timed-pulse the food gate
  directly from the dashboard.
- **Water pump dosing** — request an **exact volume in mL** (quick presets
  5/10/15/25/50 or a custom value). The Arduino runs the pump under closed-loop
  flow-sensor feedback with early-stop coast compensation and a siphon-prevention
  valve, stopping at the calibrated target.
- **Mouse platform control** — a directional (RC-style) control panel.

#### 🖥 System
Operations and diagnostics without needing SSH:
- Live **service health** (backend, camera service, ingester, bridge).
- **Raspberry Pi telemetry**: CPU temperature, fan level, memory, uptime.
- **Deploy history** with clickable commit SHAs, and a filterable, colour-coded
  tail of the deploy log.
- **AI model** summary and **security posture**.
- **Reboot / shutdown** controls for the Pi.

#### ⚙ Settings
- Interface **language**.
- Signed-in **account** details.
- Live **deployment** info (backend version, environment, API base URL).
- **API key** management — create long-lived `ivc_…` keys so devices/scripts can
  push sensor data to the ingest endpoint without a user login.

#### 👥 Team / About
Project description, the team, and the technology overview.

### 1.6 The end-to-end control loop (what happens behind the scenes)

```
 Sensors/actuators (Arduino Mega)  ── USB ──▶  Raspberry Pi bridge
        ▲                                            │ POST /api/v1/ingest
        │ commands (servo/pump/dose)                 ▼
   /tmp command queue  ◀── camera service ◀──  FastAPI backend (+ SQLite)
                                                     │  WebSocket + REST
                                                     ▼
                                              React dashboard (browser)
```

So a researcher action in the browser (e.g. "dose 15 mL", "open feed gate",
"set camera brightness") travels backend → Pi → Arduino, and live sensor data
flows back Arduino → Pi → backend → dashboard, all in real time.

---

## Part 2 — Install & run from source (reproduce the demo)

This is a "Getting Started" guide assuming a **fresh PC**. You can reproduce the
**entire dashboard demo without any hardware** — a built-in **simulator**
publishes realistic sensor data so every screen, chart, alert and export works.

### 2.1 What you need

| Requirement | Version | Notes |
|---|---|---|
| **git** | any recent | to clone the repo |
| **Docker Desktop** *(recommended path)* | current | includes Docker Compose v2 |
| — *or, for the manual path* — | | |
| **Python** | 3.11 – 3.13 | backend (FastAPI) |
| **Node.js** | 20.x | frontend (Vite + React) |

No database server is required — the backend uses **SQLite**, created
automatically on first boot.

### 2.2 Get the source

```bash
git clone https://github.com/MustafazadaAghasalim/industryprojectfinal.git
cd industryprojectfinal
```

### 2.3 Configuration

All configuration is via a single `.env` file at the repo root. Create it from
the template:

```bash
cp .env.example .env
```

Then edit `.env` — the **only value you must change** is the JWT secret:

```ini
# --- Backend ---
DATABASE_URL=sqlite:///./data/ivc.db
JWT_SECRET=replace-with-a-strong-random-string-min-32-chars   # ← set this (32+ chars)
JWT_EXPIRE_MINUTES=720

# Seeded accounts (created on first boot only)
DEFAULT_RESEARCHER_USER=researcher
DEFAULT_RESEARCHER_PASSWORD=change-me-please
DEFAULT_ADMIN_USER=admin
DEFAULT_ADMIN_PASSWORD=change-me-please

# --- Frontend ---
VITE_API_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000/ws
VITE_PI_STREAM_URL=http://192.168.50.2:8090   # only relevant with a real Pi/cameras
```

> The backend refuses to boot in production with the default dev JWT secret —
> always set a real 32+ character `JWT_SECRET`.

### 2.4 Path A — Docker Compose (recommended, one command)

This builds and starts the **backend**, **frontend**, and **simulator** together.

```bash
./scripts/dev-up.sh
# (equivalent to: docker compose up --build)
```

Then open:

| Service | URL |
|---|---|
| **Dashboard** | <http://localhost:5173> |
| Backend API + Swagger docs | <http://localhost:8000/docs> |
| Backend health check | <http://localhost:8000/health> |

Sign in with `researcher` / `change-me-please`. The **simulator** immediately starts
publishing synthetic readings, so the Overview, charts, alerts and exports are
all populated with live-looking data — no hardware needed.

To stop: `Ctrl-C`, or `docker compose down`.

### 2.5 Path B — Manual (three terminals, no Docker)

**Terminal 1 — backend**
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — frontend**
```bash
cd frontend
npm install
npm run dev          # serves http://localhost:5173
```

**Terminal 3 — simulator** (publishes synthetic sensor data)
```bash
cd device/simulator
pip install -r requirements.txt
python simulator.py
```

Open <http://localhost:5173> and sign in with `researcher` / `change-me-please`.

### 2.6 First-boot behaviour

- The SQLite database is **created automatically** under `backend/data/` (or a
  Docker volume) and lightweight column migrations run on startup.
- The two default accounts are **seeded** on first boot from the `.env` values.
- Two background tasks start automatically: the **aggregator** (AI behaviour
  classifier + anomaly detector) and the **feeding scheduler**.

### 2.7 Verifying it works

1. The dashboard loads and you can log in.
2. **Overview** shows at least one cage with live, changing values (from the
   simulator).
3. `http://localhost:8000/health` returns `{"status":"ok",...}`.
4. `http://localhost:8000/docs` shows the interactive OpenAPI documentation.
5. Charts on a cage's detail page populate after a minute of simulated data.

### 2.8 Optional — running with the real hardware

The demo runs fully on the simulator, but to reproduce the **physical** cage:

- **Arduino Mega 2560** firmware: `device/arduino/ivc_sensors/` — compile/flash
  with `arduino-cli` (FQBN `arduino:avr:mega`). Pin map and wiring are in
  [`docs/HARDWARE.md`](HARDWARE.md) and [`docs/BILL_OF_MATERIALS.md`](BILL_OF_MATERIALS.md).
- **Raspberry Pi services** (run as `systemd` units): the **Arduino bridge**
  (`device/arduino-bridge/`), the **camera service** (`device/camera-stream/`),
  the **environment ingester** (`device/env-ingester/`), and **on-Pi inference**
  (`device/pi-inference/`).
- Each device folder has its own `env.example` and README. Devices push data to
  the backend using an **API key** created in the dashboard's Settings page.
- Set `VITE_PI_STREAM_URL` to the Pi's camera-service address so the dashboard's
  Cameras page can reach the live MJPEG streams.

### 2.9 How the production demo is hosted (for context)

The live demo at **example.org is entirely self-hosted on the Raspberry Pi** —
there is no Vercel/Railway. `cloudflared` on the Pi opens an outbound
**Cloudflare Tunnel** (no inbound ports, no port-forwarding) routing
`example.org → localhost:8000`, where the FastAPI backend serves the **API + the
built React SPA + the SQLite database**. A `pi-deploy` timer polls GitHub every
60 seconds and redeploys on every push to `main`, rebuilding only the subsystems
that changed.

### 2.10 Troubleshooting

| Symptom | Fix |
|---|---|
| Backend refuses to start | Set a real `JWT_SECRET` (32+ chars) in `.env`. |
| Dashboard empty / "no data" | Make sure the **simulator** is running (Path A starts it automatically). |
| Port already in use | Change `BACKEND_PORT` in `.env` or free ports 8000/5173. |
| Cameras page shows offline | Expected without a real Pi; set `VITE_PI_STREAM_URL` to a reachable camera service. |
| Login fails | Use `researcher` / `change-me-please` (or your `.env` overrides); the DB seeds these on first boot only. |

---

## Quick reference

```text
Live demo .......... https://example.org   (cameras: https://cam.example.org)
Local dashboard .... http://localhost:5173
Local API docs ..... http://localhost:8000/docs
Login .............. researcher / change-me-please
One-command run .... cp .env.example .env  &&  ./scripts/dev-up.sh
```

See also: [`README.md`](../README.md) · [`docs/HARDWARE.md`](HARDWARE.md) ·
[`docs/BILL_OF_MATERIALS.md`](BILL_OF_MATERIALS.md) ·
[`docs/architecture.md`](architecture.md) · [`docs/api-reference.md`](api-reference.md)
</content>
</invoke>
