def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_and_me(client):
    r = client.post("/api/v1/auth/login", json={"username": "researcher", "password": "change-me-please"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    r2 = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json()["username"] == "researcher"


def test_cages_list_requires_auth(client):
    r = client.get("/api/v1/cages")
    assert r.status_code == 401


def test_ingest_round_trip(client):
    r = client.post("/api/v1/auth/login", json={"username": "researcher", "password": "change-me-please"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    cages = client.get("/api/v1/cages", headers=headers).json()
    assert cages, "seed should have created demo cages"
    cage_id = cages[0]["id"]

    payload = {
        "cage_id": cage_id,
        "ts": "2026-05-18T20:14:31Z",
        "seq": 1,
        "sensors": {
            "feeding":   {"delivered_g": 1.42, "remaining_g": 18.3, "wasted_g": 0.05, "gate_state": "open"},
            "water":     {"flow_ml": 0.18, "tank_ml": 198.4},
            "metabolic": {"vo2_ml_min_kg": 38.9, "vco2_ml_min_kg": 31.2, "rer": 0.80, "ee_kcal_h": 0.42},
            "behaviour": {"grid_cells": [0]*16, "movement_cm": 4.2},
            "weighing":  {"animal_g": 24.8},
        },
    }
    r = client.post("/api/v1/ingest", json=payload, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json()["stored"] == 5

    readings = client.get(f"/api/v1/cages/{cage_id}/readings", headers=headers).json()
    assert len(readings) >= 5
