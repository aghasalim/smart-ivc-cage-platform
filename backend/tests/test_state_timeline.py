import time
from datetime import datetime, timezone

import pytest


@pytest.fixture
def brussels_tz(monkeypatch):
    # SQLite hands timestamps back without a timezone; on a machine that is not on
    # UTC that used to shift the ethogram by the local offset
    monkeypatch.setenv("TZ", "Europe/Brussels")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_state_timeline_buckets_in_utc(client, brussels_tz):
    from app.db import SessionLocal
    from app.models import MouseStateSample

    token = client.post("/api/v1/auth/login",
                        json={"username": "researcher", "password": "change-me-please"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    cage_id = client.get("/api/v1/cages", headers=headers).json()[0]["id"]

    with SessionLocal() as db:
        db.add(MouseStateSample(cage_id=cage_id, mouse_id="A", state="Possible Sleep",
                                ts=datetime(2026, 5, 18, 0, 10, tzinfo=timezone.utc)))
        db.commit()

    r = client.get(f"/api/v1/cages/{cage_id}/state-timeline",
                   params={"date": "2026-05-18", "bucket": 300, "mouse": "A"}, headers=headers)
    assert r.status_code == 200, r.text
    segs = r.json()["mice"][0]["segments"]
    assert segs == [{"t": "2026-05-18T00:10:00Z", "state": "Possible Sleep"}]
