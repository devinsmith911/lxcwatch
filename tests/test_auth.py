import base64
import dataclasses
import importlib
import os
import time

import pytest

from lxcwatch import config
from lxcwatch.auth import COOKIE, MAX_FAILURES, Auth


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("LXCWATCH_PVE_HOST", "192.0.2.1")
    monkeypatch.setenv("LXCWATCH_PASSWORD", "s3cret")
    monkeypatch.setenv("LXCWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LXCWATCH_SCAN_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("LXCWATCH_SCAN_ON_STARTUP", "never")
    return config.load()


def test_session_roundtrip_and_tampering(cfg):
    auth = Auth(cfg)
    token, max_age = auth.issue()
    assert max_age == 30 * 86400
    assert auth.check_session(token)
    user, expiry, sig = token.split(".")
    assert not auth.check_session(f"{user}.{int(expiry) + 999}.{sig}")  # extended expiry
    assert not auth.check_session(f"mallory.{expiry}.{sig}")
    assert not auth.check_session(None) and not auth.check_session("garbage")
    assert oct(os.stat(os.path.join(cfg.data_dir, "session.key")).st_mode & 0o777) == "0o600"


def test_session_expires_and_password_change_signs_out(cfg, monkeypatch):
    auth = Auth(cfg)
    token, _ = auth.issue()
    monkeypatch.setattr(time, "time", lambda: 10**12)
    assert not auth.check_session(token)
    monkeypatch.undo()
    changed = Auth(dataclasses.replace(cfg, password="new-password"))
    assert not changed.check_session(token)
    assert Auth(cfg).check_session(token)  # same secret file, same password: still valid


def test_lockout(cfg):
    auth = Auth(cfg)
    for _ in range(MAX_FAILURES):
        assert not auth.locked_out("1.2.3.4")
        auth.record_failure("1.2.3.4")
    assert auth.locked_out("1.2.3.4") and not auth.locked_out("5.6.7.8")


@pytest.fixture
def client(cfg):
    from fastapi.testclient import TestClient
    import lxcwatch.app as app_module
    app_module = importlib.reload(app_module)
    return TestClient(app_module.app, base_url="http://testserver")


H = {"X-Requested-With": "lxcwatch"}


def test_login_flow(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/jobs?x=1", follow_redirects=False).headers["location"] == "/login?next=%2Fjobs%3Fx%3D1"
    assert client.get("/api/containers").status_code == 401
    assert "www-authenticate" not in client.get("/api/containers").headers  # no browser popup
    assert client.get("/login").status_code == 200
    assert client.get("/icon.svg").status_code == 200

    assert client.post("/api/login", json={"username": "admin", "password": "s3cret"}).status_code == 403  # no header
    assert client.post("/api/login", json={"username": "admin", "password": "nope"}, headers=H).status_code == 401
    r = client.post("/api/login", json={"username": "admin", "password": "s3cret"}, headers=H)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=2592000" in cookie

    assert client.get("/api/containers").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303  # already signed in
    client.post("/api/logout", headers=H)
    client.cookies.clear()
    assert client.get("/api/containers").status_code == 401


def test_basic_auth_still_works(client):
    good = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    bad = "Basic " + base64.b64encode(b"admin:wrong").decode()
    assert client.get("/api/containers", headers={"Authorization": good}).status_code == 200
    assert client.get("/api/containers", headers={"Authorization": bad}).status_code == 401


def test_login_lockout(client):
    for _ in range(MAX_FAILURES):
        client.post("/api/login", json={"username": "admin", "password": "x"}, headers=H)
    r = client.post("/api/login", json={"username": "admin", "password": "s3cret"}, headers=H)
    assert r.status_code == 429
    assert COOKIE not in r.headers.get("set-cookie", "")
