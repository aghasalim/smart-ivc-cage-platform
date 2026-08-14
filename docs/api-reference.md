# API reference

| Environment | Base URL |
|---|---|
| **Production** | `https://example.org` |
| Camera streams | `https://cam.example.org` |
| Development | `http://localhost:8000` |

All times are ISO-8601 UTC. All request and response bodies are JSON unless
otherwise noted. The interactive OpenAPI/Swagger UI is at
[`/docs`](https://example.org/docs).

---

## Authentication

The API uses **JWT bearer tokens**. Obtain one via `POST /api/v1/auth/login`,
then send it on subsequent calls:

```
Authorization: Bearer <token>
```

JWT lifetime is 12 hours. Tokens can be refreshed via
`POST /api/v1/auth/refresh` while still valid. The login endpoint is
**rate-limited** and uses **constant-time** password verification to
mitigate timing attacks.

The same JWT also authenticates the **WebSocket** connection (passed as
the `token` query parameter — see § WebSocket below).

---

## Health & meta

### `GET /health`

Liveness probe. No auth required.

```json
{ "status": "ok", "version": "1.0.0" }
```

### `GET /api/v1/meta`

Returns build metadata and the public configuration. No auth required.

```json
{
  "version": "1.0.0",
  "env": "production",
  "feature_flags": { "alerts": true, "reports": true, "ml": true }
}
```

---

## Authentication endpoints

### `POST /api/v1/auth/login`

```json
// request
{ "username": "researcher", "password": "change-me-please" }

// 200 OK
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 43200
}
```

### `POST /api/v1/auth/refresh`

Refreshes a valid token (issues a new one with a fresh expiry).
Requires a valid `Authorization: Bearer …` header.

### `GET /api/v1/auth/me`

Returns the current user.

```json
{ "id": 1, "username": "researcher", "role": "researcher" }
```

---

## Cages

### `GET /api/v1/cages`

Lists every cage visible to the caller.

```json
[
  {
    "id": "cage-001",
    "name": "Cohort A — male B6 #1",
    "location": "Rack 3, shelf 2",
    "animal_strain": "C57BL/6J",
    "animal_age_days": 84,
    "created_at": "2026-04-01T09:00:00Z",
    "last_seen": "2026-05-20T20:12:11Z"
  }
]
```

### `POST /api/v1/cages` *(admin)*

Creates a new cage.

### `GET /api/v1/cages/{cage_id}`

Detailed view, including current sensor snapshot.

### `PATCH /api/v1/cages/{cage_id}`

Updates metadata (name, location, strain, age).

### `DELETE /api/v1/cages/{cage_id}` *(admin)*

Removes a cage. Readings are kept and orphaned-flagged.

---

## Ingest (device only)

### `POST /api/v1/ingest`

The only endpoint a cage controller talks to. One packet per 5 s.
Every sensor key is **optional** — a packet may carry just the
`environment` reading (as the env-ingester does) or a subset of sensors.

```json
// request
{
  "cage_id": "cage-001",
  "ts": "2026-05-20T20:14:31Z",
  "seq": 12345,
  "sensors": {
    "feeding":     { "delivered_g": 1.42, "remaining_g": 18.3, "wasted_g": 0.05, "gate_state": "open" },
    "water":       { "flow_ml":     0.18, "tank_ml":   198.4 },
    "metabolic":   { "vo2_ml_min_kg": 38.9, "vco2_ml_min_kg": 31.2, "rer": 0.80, "ee_kcal_h": 0.42 },
    "behaviour":   { "grid_cells": [0,0,0,1,2,0,0,1,3,0,0,0,0,0,0,0], "movement_cm": 4.2 },
    "weighing":    { "animal_g":   24.8 },
    "environment": { "temperature_c": 22.4, "humidity_pct": 51.0 }
  }
}

// 202 Accepted
{ "received": true, "stored": 6 }
```

Rejected if:

| Status | Cause |
|---|---|
| 401 | Bad or missing token |
| 404 | Cage unknown |
| 409 | Duplicate `seq` for the same `(cage, sensor)` |
| 422 | Schema violation |

Replay protection: the database has a unique constraint on
`(cage_id, sensor, seq)`, so a replay of an old packet returns 409 instead
of double-counting.

---

## Readings

### `GET /api/v1/cages/{cage_id}/readings`

Query parameters:

| Parameter | Type | Description |
|---|---|---|
| `sensor` | `feeding \| water \| metabolic \| behaviour \| weighing \| environment` | Filter to one sensor |
| `from` | ISO-8601 | Inclusive lower bound |
| `to` | ISO-8601 | Inclusive upper bound |
| `limit` | int (≤ 5000) | Max rows returned |

```json
[
  { "ts": "2026-05-20T20:14:31Z", "sensor": "metabolic", "payload": { "rer": 0.80, "vo2_ml_min_kg": 38.9, ... } },
  { "ts": "2026-05-20T20:14:30Z", "sensor": "environment", "payload": { "temperature_c": 22.4, "humidity_pct": 51.0 } }
]
```

### `GET /api/v1/cages/{cage_id}/summary`

Daily summary for the last `N` days.

```json
{
  "cage_id": "cage-001",
  "days": [
    {
      "date": "2026-05-19",
      "feeding": { "intake_g": 4.6, "delivered_g": 5.1, "wasted_g": 0.5 },
      "water_ml": 5.2,
      "metabolic": { "avg_rer": 0.83, "ee_kcal": 9.9 },
      "behaviour": { "active_minutes": 312, "sleeping_minutes": 1128 }
    }
  ]
}
```

### `GET /api/v1/cages/{cage_id}/export.csv`

CSV export over a date range. One row per reading. Returned with
`Content-Type: text/csv` and a `Content-Disposition: attachment` header so
browsers download it directly.

---

## Alerts

### `GET /api/v1/alerts`

Query: `?acknowledged=<true|false>&limit=<n>`

```json
[
  {
    "id": 42,
    "cage_id": "cage-001",
    "ts": "2026-05-20T08:15:02Z",
    "severity": "warning",
    "rule": "anomaly_rer",
    "message": "Anomaly: RER 1.18 vs baseline 0.85 ± 0.04 (z=+8.25)",
    "acknowledged_at": null
  }
]
```

Possible `rule` values:

| Rule | Source | Severity |
|---|---|---|
| `rer_out_of_range` | Threshold rule | warning |
| `intake_low_24h` | Threshold rule | warning |
| `no_activity` | Threshold rule | critical |
| `anomaly_rer` | AI model 2 (anomaly detector) | info / warning |
| `anomaly_movement_cm` | AI model 2 | info / warning |
| `anomaly_water_flow_ml` | AI model 2 | info / warning |

### `POST /api/v1/alerts/{id}/ack`

Acknowledges an alert (sets `acknowledged_at = now`).

---

## Schedule

### `GET /api/v1/cages/{cage_id}/schedule`

```json
{
  "cage_id": "cage-001",
  "windows": [
    { "id": 1, "start_local": "23:00", "end_local": "00:30", "days": [0,1,2,3,4,5,6], "active": true }
  ]
}
```

### `PUT /api/v1/cages/{cage_id}/schedule`

Replaces the schedule for a cage.

---

## System / operations

These endpoints back the dashboard's `/system` page. All require auth.

### `GET /api/v1/system/status`

Single-call snapshot of the runtime: versions, services, network
endpoints, deploy pipeline, AI models, and security posture.

```json
{
  "app":      { "name": "IVC Cage Backend", "version": "1.0.0", "env": "production", "git_sha": null },
  "ai_models": {
    "behaviour_classifier": { "type": "discriminative",   "purpose": "...", "labels":  ["sleeping", "..."] },
    "anomaly_detector":     { "type": "streaming MAD",    "purpose": "...", "metrics": ["rer", "..."], "cages_warm": 0 }
  },
  "runtime":  { "uptime_s": 41.2, "python": "3.13.5", "platform": "Linux aarch64", "hostname": "pi", "pid": 24834 },
  "services": { "ivc-backend": "active", "ivc-cameras": "active", "ivc-env-ingester": "active" },
  "endpoints":{ "public": "https://example.org", "cameras": "https://cam.example.org", "api_docs": "/docs", "health": "/health" },
  "deploy":   { "auto_deploy": true, "source": "github.com/MustafazadaAghasalim/industryprojectfinal",
                "branch": "main", "poll_interval_s": 60, "last_deploy_ts": 1779285881.8, "deploy_log_available": true },
  "security": { "https": true, "hsts": true, "csp": true, "rate_limited_endpoints": ["/api/v1/auth/login"], "jwt_expire_minutes": 720 }
}
```

### `GET /api/v1/system/logs`

Query: `?lines=<1..500>` (default 80)

Returns the last `lines` entries of `~/pi-deploy.log`, parsed into
`{ ts, msg }` objects.

```json
{
  "available": true,
  "path": "/home/grazwis/pi-deploy.log",
  "lines": [
    { "ts": "2026-05-20T16:17:07+02:00", "msg": "Deploy complete (22a42ee)" },
    { "ts": "2026-05-20T16:17:05+02:00", "msg": "Syncing frontend dist → static/" }
  ]
}
```

### `GET /api/v1/system/deploys`

Query: `?limit=<1..50>` (default 10)

Returns recent successful deploys parsed from the deploy log.

```json
{
  "available": true,
  "deploys": [
    { "ts": "2026-05-20T16:17:07+02:00", "sha": "22a42ee" },
    { "ts": "2026-05-20T16:08:19+02:00", "sha": "91eb06b" }
  ]
}
```

### `GET /api/v1/system/anomaly`

Inspect the AnomalyDetector's per-cage baselines (useful for debugging).

```json
{
  "model": {
    "type": "streaming MAD",
    "metrics": ["rer", "movement_cm", "water_flow_ml"],
    "z_info_threshold": 3.0,
    "z_warn_threshold": 4.5,
    "window_samples": 240,
    "warmup_samples": 24
  },
  "baselines": {
    "cage-001": {
      "rer":            { "samples": 240, "warm": true, "median": 0.85, "mad": 0.04 },
      "movement_cm":    { "samples": 240, "warm": true, "median": 3.20, "mad": 1.20 }
    }
  }
}
```

---

## WebSocket — real-time stream

### `wss://example.org/ws?token=<jwt>`

After connecting, the server emits one JSON message per new event:

```json
{ "type": "reading", "cage_id": "cage-001", "sensor": "metabolic", "ts": "...", "payload": { ... } }
{ "type": "alert",   "cage_id": "cage-001", "severity": "warning", "rule": "anomaly_rer", "message": "..." }
{ "type": "label",   "cage_id": "cage-001", "ts": "...", "behaviour": "sleeping", "confidence": 0.87 }
```

Clients can filter with a subscribe message:

```json
{ "action": "subscribe", "cage_ids": ["cage-001"], "sensors": ["metabolic", "behaviour"] }
```

The dashboard subscribes automatically when a researcher logs in.

---

## Error format

Every error returns the same shape:

```json
{ "error": { "code": "INVALID_TOKEN", "message": "Signature has expired." } }
```

| HTTP | Code | Meaning |
|---|---|---|
| 400 | `BAD_REQUEST` | Generic client error |
| 401 | `INVALID_CREDENTIALS` / `INVALID_TOKEN` | Auth failure |
| 403 | `FORBIDDEN` | Authenticated but lacking permission |
| 404 | `NOT_FOUND` | Cage / alert / resource missing |
| 409 | `CONFLICT` | Duplicate `seq` on ingest |
| 422 | `VALIDATION_FAILED` | Body didn't match schema |
| 429 | `RATE_LIMITED` | Rate limiter triggered (e.g. login bursts) |
| 500 | `INTERNAL` | Unhandled server error (logged with traceback) |

Validation errors **never** echo the request body — the field set is
returned generically to avoid leaking credentials or PII.
