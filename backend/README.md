# Backend — FastAPI + SQLite + JWT + WebSocket

The data hub of the IVC platform. Receives sensor packets from cage controllers, persists them, runs ML behaviour classification, fans out updates over WebSocket, and serves a REST API to the dashboard.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open http://localhost:8000/docs for the auto-generated OpenAPI UI.

## Project layout

```
app/
├── main.py               ← FastAPI factory + lifespan
├── config.py             ← Pydantic settings, env-driven
├── logging_config.py     ← Structured JSON logging
├── db.py                 ← SQLAlchemy engine + session
├── models.py             ← ORM tables
├── schemas.py            ← Pydantic request/response models
├── security.py           ← JWT, password hashing, dependencies
├── seed.py               ← First-boot seed (users, demo cages)
├── api/
│   ├── auth.py           ← /api/v1/auth/*
│   ├── cages.py          ← /api/v1/cages/*
│   ├── ingest.py         ← /api/v1/ingest
│   ├── readings.py       ← /api/v1/cages/{id}/readings + summary + export
│   ├── alerts.py         ← /api/v1/alerts/*
│   └── schedule.py       ← /api/v1/cages/{id}/schedule
├── services/
│   ├── aggregator.py     ← Rolling-window features + ML inference
│   ├── rules.py          ← Alert rule engine
│   └── broadcaster.py    ← WebSocket fan-out
├── ai/
│   └── classifier.py     ← Loads sklearn model, exposes .predict()
└── ws.py                 ← WebSocket endpoint
```

## Tests

```bash
pip install pytest httpx
pytest
```
