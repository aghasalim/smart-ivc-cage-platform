"""A minimal client for the cage API. Standard library only.

    python examples/client.py                  # http://localhost:8000
    API=https://your-host python examples/client.py

Copy the class into your own script, or use it as a reference for the
request shapes; docs/api-reference.md has every endpoint.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class CageClient:
    def __init__(self, base: str, username: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.token = self._call("POST", "/api/v1/auth/login", {"username": username, "password": password})["access_token"]

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("Content-Type", "application/json")
        if getattr(self, "token", None):
            req.add_header("Authorization", f"Bearer {self.token}")
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(req, data, timeout=10) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()}") from None

    def cages(self) -> list[dict]:
        return self._call("GET", "/api/v1/cages")

    def ingest(self, cage_id: str, seq: int, sensors: dict) -> dict:
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return self._call("POST", "/api/v1/ingest", {"cage_id": cage_id, "ts": ts, "seq": seq, "sensors": sensors})

    def readings(self, cage_id: str, limit: int = 10) -> list[dict]:
        return self._call("GET", f"/api/v1/cages/{cage_id}/readings?limit={limit}")

    def summary(self, cage_id: str) -> dict:
        return self._call("GET", f"/api/v1/cages/{cage_id}/summary")


if __name__ == "__main__":
    api = CageClient(os.environ.get("API", "http://localhost:8000"), "researcher", os.environ.get("PASS", "change-me-please"))
    cages = api.cages()
    if not cages:
        sys.exit("no cages yet; run examples/quickstart.sh or start the simulator")
    cage = cages[0]["id"]
    packet = json.loads((Path(__file__).parent / "ingest_packet.json").read_text())
    print("ingest ->", api.ingest(cage, seq=int(datetime.now().timestamp()), sensors=packet["sensors"]))
    for r in api.readings(cage, limit=3):
        print(r["ts"], r["sensor"], r["payload"])
    assert api.readings(cage, limit=1), "a reading we just posted should come back"
    print("ok")
