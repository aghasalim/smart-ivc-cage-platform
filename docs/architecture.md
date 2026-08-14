# Architecture

> **Companion doc:** [`HARDWARE.md`](HARDWARE.md) — full hardware
> technical analysis (BOM, wiring, protocols, failure modes).
> This document focuses on the *software* architecture.

## 1. Overview

```
                          ┌──────────────────────────────┐
                          │       Internet user           │
                          │  https://example.org          │
                          │  https://cam.example.org      │
                          └──────────────┬───────────────┘
                                         │  HTTPS / WSS
                                         ▼
                         ┌──────────────────────────────┐
                         │   Cloudflare Tunnel edge      │
                         │ (no inbound ports on the Pi)  │
                         └──────────────┬───────────────┘
                                        │  encrypted outbound
                                        ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │                  Raspberry Pi 5  (the "device")                   │
 │  ──────────────────────────────────────────────────────────────  │
 │  systemd user services — auto-restart, no Docker                  │
 │                                                                    │
 │   ┌─────────────┐  ┌──────────┐  ┌────────────┐  ┌────────────┐  │
 │   │   REST API  │→ │  Service │→ │ SQLAlchemy │→ │ SQLite WAL │  │
 │   │   /ingest   │  │   layer  │  │   ORM      │  │ ivc.db     │  │
 │   └─────────────┘  └────┬─────┘  └────────────┘  └────────────┘  │
 │                          │                                         │
 │                          ▼                                         │
 │   ┌────────────────────────────────────────────────────────────┐  │
 │   │   Aggregator loop (every 30 s) — runs TWO AI models:       │  │
 │   │   1. BehaviourClassifier (HistGradientBoosting)            │  │
 │   │   2. AnomalyDetector  (streaming MAD per cage / metric)    │  │
 │   └────────────────────────────┬───────────────────────────────┘  │
 │                                │                                   │
 │                                ▼                                   │
 │   ┌──────────────┐    ┌────────────────────┐  ┌────────────────┐ │
 │   │  Rule engine │ →  │ WebSocket fan-out  │→ │ Browser SPA    │ │
 │   │  (Alerts)    │    │  /ws  (JSON)       │  │ React 18+Vite  │ │
 │   └──────────────┘    └────────────────────┘  └────────────────┘ │
 │                                                                    │
 │   USB-serial 115 200 8N1                  USB-V4L2                │
 │   ┌────────────────┐                  ┌─────────────────┐         │
 │   │ Arduino Mega   │                  │  USB cameras    │         │
 │   │  • feed servo  │                  │  (1..N)         │         │
 │   │  • water servo │                  │  ffmpeg readers │         │
 │   │  • DHT11 (T/H) │                  │  + frame cache  │         │
 │   └────────────────┘                  └─────────────────┘         │
 └──────────────────────────────────────────────────────────────────┘
```

## 2. Process inventory (on the Pi)

| Process | Purpose | Listening on | Restart policy |
|---|---|---|---|
| `cloudflared` | Outbound tunnel for example.org + cam.example.org | — (outbound only) | system service, `Restart=on-failure` |
| `ivc-backend` | FastAPI app — REST + WebSocket + AI + rules | `127.0.0.1:8000` | systemd user, on-failure |
| `ivc-cameras` | V4L2 → MJPEG bridge, servo proxy, DHT11 proxy | `127.0.0.1:8090` | systemd user, on-failure |
| `ivc-env-ingester` | Polls camera service, posts DHT11 → backend | — (HTTP client) | systemd user, on-failure |
| `pi-deploy.timer` | 60 s GitHub poll → `pi-deploy.sh` | — | systemd user timer, `Persistent=true` |

All five share **structured JSON logging** to journald + (for the deploy
job) `~/pi-deploy.log` which is tailed by the dashboard's `/system`
page.

## 3. Module map (backend/)

```
backend/app/
├── main.py              ── FastAPI app, lifespan, CORS, security headers
├── config.py            ── env-driven settings (Pydantic Settings)
├── db.py                ── SQLAlchemy engine, WAL checkpoint, session factory
├── models.py            ── User, Cage, Reading, BehaviourLabel, Alert,
│                          ScheduleWindow, AuditLog
├── schemas.py           ── Pydantic v2 request/response models
├── security.py          ── JWT, bcrypt, get_current_user
├── middleware.py        ── OWASP security headers + CSP per-path
├── limiter.py           ── slowapi rate limiter (login is rate-limited)
├── logging_config.py    ── structured JSON logging
├── seed.py              ── First-boot seed (default researcher + cages)
├── ws.py                ── WebSocket router
│
├── api/
│   ├── auth.py          ── /api/v1/auth/{login, refresh, me}
│   ├── cages.py         ── /api/v1/cages, /cages/{id}/...
│   ├── ingest.py        ── /api/v1/ingest  (device → backend)
│   ├── readings.py      ── /api/v1/cages/{id}/{readings,summary,export.csv}
│   ├── alerts.py        ── /api/v1/alerts
│   ├── schedule.py      ── /api/v1/cages/{id}/schedule
│   └── system.py        ── /api/v1/system/{status, logs, deploys, anomaly}
│
├── services/
│   ├── aggregator.py    ── 30 s loop — invokes BOTH AI models
│   ├── broadcaster.py   ── WebSocket fan-out queue
│   └── rules.py         ── Threshold rules (intake, RER, no_activity)
│
└── ai/
    ├── classifier.py    ── Model 1 — behaviour classifier (sklearn pickle)
    └── anomaly.py       ── Model 2 — streaming MAD anomaly detector
```

## 4. Data flow

### 4.1 Sensor → DB (device-side packets)

The cage's camera-stream service produces sensor packets every 5 s. The
**env-ingester** publishes them to the backend over the loopback
interface, authenticated with a JWT:

```json
POST /api/v1/ingest
Authorization: Bearer <jwt>
{
  "cage_id": "cage-001",
  "ts":      "2026-05-20T16:14:31Z",
  "seq":     12345,
  "sensors": {
    "feeding":     { "delivered_g": 1.42, "remaining_g": 18.3, "wasted_g": 0.05, "gate_state": "open" },
    "water":       { "flow_ml":     0.18, "tank_ml":   198.4 },
    "metabolic":   { "vo2_ml_min_kg": 38.9, "vco2_ml_min_kg": 31.2, "rer": 0.80, "ee_kcal_h": 0.42 },
    "behaviour":   { "grid_cells": [0,0,0,1,2,0,0,1,3,0,0,0,0,0,0,0], "movement_cm": 4.2 },
    "weighing":    { "animal_g":  24.8 },
    "environment": { "temperature_c": 22.4, "humidity_pct": 51.0 }
  }
}
```

1. **Validation** — Pydantic v2 schemas, each sensor is optional.
2. **Dedup** — `UniqueConstraint(cage_id, sensor, seq)` rejects replays.
3. **Persistence** — one append-only row per sensor in the `readings` table.
4. **Rules** — `evaluate_rules(...)` checks thresholds inline, creates alerts.
5. **Broadcast** — every new reading + alert is pushed to WebSocket subscribers.

### 4.2 AI pipeline (aggregator-side)

```
        every 30 s tick
              │
              ▼
   _build_features(cage)         ← grid + movement + weight + gate + hour
              │
              ├──────────────────► classifier.predict()
              │                       │
              │                       ▼
              │            INSERT behaviour_labels
              │            WS  "label"  event
              │
              └──────────────────► anomaly_detector.observe(metric, value)
                                        │
                                        ▼
                              if |z| ≥ threshold:
                                  INSERT alerts(rule="anomaly_<metric>")
                                  WS  "alert"  event
```

Two models, two different jobs:

* **BehaviourClassifier** is *discriminative* — it always picks one of
  the seven behaviour labels for every window.
* **AnomalyDetector** is *generative-ish, online, unsupervised* — it learns
  each cage's baseline for RER / movement / water_flow and flags windows
  whose robust z-score exceeds 3 σ (info) or 4.5 σ (warning). MAD-based,
  so anomalies don't poison their own baseline.

## 5. Deployment topology

### 5.1 Development (no hardware)

```
  ┌─────────────────────────────────────────────┐
  │  Developer laptop                            │
  │                                              │
  │   ./scripts/dev-up.sh                        │
  │     ├── uvicorn backend.app.main:app         │
  │     ├── vite (frontend dev server)           │
  │     └── python device/simulator/simulator.py │
  │                                              │
  │   http://localhost:5173                      │
  └─────────────────────────────────────────────┘
```

### 5.2 Production — single-cage prototype (current)

```
  ┌──────────────────────────────────────────────────────────────┐
  │ Internet                                                       │
  │      │                                                         │
  │  Cloudflare Tunnel  (no inbound ports on the Pi)              │
  │      │                                                         │
  │      ▼                                                         │
  │  ┌────────────────────────────────────────────────────────┐   │
  │  │ Raspberry Pi 5 (Howest-IoT WiFi or LAN cable)          │   │
  │  │   ivc-backend (FastAPI · uvicorn)                       │   │
  │  │   ivc-cameras (V4L2 + servo + DHT11 proxy)              │   │
  │  │   ivc-env-ingester (loopback poll)                      │   │
  │  │   SQLite WAL @ ~/ivc-backend/data/ivc.db                │   │
  │  │   pi-deploy.timer (GitHub poll, 60 s)                   │   │
  │  └────────────────────────────────────────────────────────┘   │
  │                                                                 │
  │  Cage hardware: Arduino Mega (servos + DHT11) · USB cameras    │
  └──────────────────────────────────────────────────────────────┘
```

### 5.3 Production — multi-cage future state

```
                          Cloudflare
                              │
              ┌───────────────┼──────────────┐
              ▼               ▼              ▼
           Pi A             Pi B           Pi C        … one Pi per rack
           cages 1–4        cages 5–8      cages 9–12
              \              │              /
               \             │             /
                └─── central data sync (optional) ──→ shared SQLite mirror
```

Because each Pi is fully self-contained (SQLite, WS, dashboard, AI), a
multi-cage rollout is purely additive — no central infrastructure required.

## 6. Configuration & secrets

* All configuration is environment-variable driven (Pydantic Settings).
* Public URLs live in `frontend/.env.production` (committed — not secret).
* Real secrets (JWT signing key, DB passwords if any) live in `.env`,
  which is `.gitignore`d and provisioned on the Pi out-of-band.
* The backend **refuses to boot** in production with the default dev JWT secret.

## 7. Observability

| Surface | Where |
|---|---|
| Structured JSON logs | `journalctl --user -u ivc-backend` |
| Deploy history | `~/pi-deploy.log` (tailed live at `/system` page) |
| Liveness | `GET /health` |
| Build metadata | `GET /api/v1/meta` |
| Full system snapshot | `GET /api/v1/system/status` (requires auth) |
| AI baselines | `GET /api/v1/system/anomaly` (requires auth) |

The `/system` page in the dashboard surfaces all of these without SSH.

## 8. Backups & data safety

* **SQLite WAL mode** with `checkpoint_wal()` on boot — clears stale locks
  left by a crash.
* **Daily snapshot** via `sqlite3 .backup` (cron on the Pi); kept 14 days.
* **Repo backup**: all code on GitHub; the Pi can be rebuilt from `git clone`
  + the one-shot bootstrap in [`HARDWARE.md`](HARDWARE.md) §11.

## 9. Upgrade strategy

* **Code**: `git push origin main` — `pi-deploy.timer` fires within 60 s.
  `pi-deploy.sh` rsyncs only the changed subsystem and restarts only those
  services. Failed deploys never down the running services.
* **Database**: SQLAlchemy `create_all()` is additive; for schema changes
  beyond add-column we'd switch to Alembic (planned for multi-cage).
* **Firmware (Arduino)**: separate process — `avrdude` over USB from the
  Pi. Documented in `device/arduino/` and [`HARDWARE.md`](HARDWARE.md) §11.

## 10. Why these choices (rationale)

| Decision | Rationale |
|---|---|
| **SQLite WAL over Postgres** | One process, no network, perfect for an edge device; readings table handles 10⁵+ rows/day with indexed `(cage_id, sensor, ts)` |
| **systemd user services over Docker** | Sub-second boot, native journal access, no daemon-in-daemon overhead on the Pi; trivial to inspect with `systemctl --user status` |
| **Cloudflare Tunnel over port-forwarding** | No inbound ports open on the lab/home LAN; survives ISP IP changes; no router config |
| **Pull-based auto-deploy over push** | Pi never exposes a webhook endpoint; tolerates ISP outages (catches up on next tick) |
| **Two AI models** | Classifier always picks a label (even bad ones); anomaly detector flags windows that don't fit any baseline — together they catch both expected drift and unexpected health events |
| **JSON line protocol on Arduino** | Debuggable with `cat /dev/ttyACM0`, future-proof to add new commands without changing the parser |
