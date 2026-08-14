# Device simulator

Emulates one or more IVC cages. Publishes realistic sensor packets to the
backend at the cadence specified in `SIM_INTERVAL_SECONDS` (default 5 s).

## Run locally

```bash
pip install -r requirements.txt
SIM_BACKEND_URL=http://localhost:8000 python simulator.py
```

A small FastAPI status page is served on port 8001 — open
http://localhost:8001/status to inspect the simulator state.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SIM_BACKEND_URL` | `http://backend:8000` | Where to POST `/api/v1/ingest` |
| `SIM_CAGE_IDS` | `cage-001,cage-002,cage-003` | Cages to emulate |
| `SIM_INTERVAL_SECONDS` | `5` | Time between packets per cage |
| `SIM_USERNAME` | `researcher` | Login used to obtain a JWT |
| `SIM_PASSWORD` | `change-me-please` | Password for that login |
| `SIM_TIME_SCALE` | `60` | Multiplier on wall time to compress a 24h cycle |

By default the simulator runs **60× faster** than wall time so the dashboard
shows a full day of behaviour every 24 minutes. Set `SIM_TIME_SCALE=1` for
realistic real-time speed.
