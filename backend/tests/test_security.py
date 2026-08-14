def test_invalid_login(client):
    r = client.post("/api/v1/auth/login", json={"username": "researcher", "password": "wrong"})
    assert r.status_code == 401
    body = r.json()
    # Generic error code: don't leak whether the username or password was wrong.
    assert body["error"]["code"] == "INVALID_CREDENTIALS"


def test_security_headers_present(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in r.headers
    assert "Permissions-Policy" in r.headers


def test_unknown_user(client):
    r = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "whatever"})
    assert r.status_code == 401


def test_admin_required(client):
    # researcher cannot create cages
    r = client.post("/api/v1/auth/login", json={"username": "researcher", "password": "change-me-please"})
    token = r.json()["access_token"]
    r = client.post(
        "/api/v1/cages",
        json={"name": "Try"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
