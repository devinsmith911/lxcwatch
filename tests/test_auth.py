import asyncio
import base64
import dataclasses
import hashlib
import hmac
import importlib
import json
import os
import re
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from lxcwatch import config
from lxcwatch.auth import ACCESS_KEYS_MAX_AGE, ACCESS_UNKNOWN_KEY_RETRY, COOKIE, MAX_FAILURES, Auth


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


def test_dashboard_files_are_served_only_after_sign_in(client):
    good = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    page = client.get("/", headers={"Authorization": good})
    assert page.status_code == 200
    paths = re.findall(r'(?:src|href)="(/(?:js|css)/[^"]+)"', page.text)
    assert "/js/main.js" in paths and "/css/app.css" in paths
    for path in paths:
        assert client.get(path, follow_redirects=False).status_code == 303
        r = client.get(path, headers={"Authorization": good})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
        assert r.headers["content-type"].split(";")[0] == ("text/css" if path.endswith(".css") else "text/javascript")


def test_every_dashboard_file_has_a_route(client):
    static = os.path.join(os.path.dirname(config.__file__), "static")
    on_disk = {f"/{d}/{name}" for d in ("css", "js", "vendor") for name in os.listdir(os.path.join(static, d)) if name.endswith((".css", ".js"))}
    routes = {r.path for r in client.app.routes}
    assert "/vendor/preact-htm.js" in on_disk and on_disk <= routes


def test_every_imported_module_is_served(client):
    good = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    seen, todo = set(), ["/js/main.js"]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        r = client.get(path, headers={"Authorization": good})
        assert r.status_code == 200, path
        if path.startswith("/js/"):
            for target in re.findall(r'from "([^"]+)"', r.text):
                todo.append(target if target.startswith("/") else "/js/" + target.removeprefix("./"))
    assert {"/js/core.js", "/js/details.js", "/vendor/preact-htm.js"} <= seen


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


def test_failure_superseded_by_later_success(client):
    import lxcwatch.app as app_module
    db = app_module.db
    db.upsert_seen(101, "pve1", "kuma", "running", "")
    fail = {"file": ".install-error.failed", "code": 1, "at": 1000, "errinfo": []}
    db.save_scan(101, {"failures": [fail], "update_mtime": 900, "vertimes": {"/root/.kuma.pending": 5000}}, None)
    [ct] = app_module.db.containers()
    assert app_module._summary(ct)["failures"][0]["superseded"] is False  # older entrypoint, pending file ignored
    db.save_scan(101, {"failures": [fail], "update_mtime": 900, "vertimes": {"/root/.kuma": 2000}}, None)
    [ct] = app_module.db.containers()
    assert app_module._summary(ct)["failures"][0]["superseded"] is True  # version written after the failure
    db.save_scan(101, {"failures": [fail], "update_mtime": 900, "vertimes": {}}, None)
    db.create_job("j1", 101, "pve1", "app", None)
    db.update_job("j1", status="success", finished_at=3000)
    [ct] = app_module.db.containers()
    assert app_module._summary(ct)["failures"][0]["superseded"] is True  # lxcwatch update succeeded later


def test_manual_snapshot_api(client, monkeypatch):
    import lxcwatch.app as app_module
    started = []
    monkeypatch.setattr(app_module.jobs, "_spawn", lambda coro: (started.append(coro), coro.close()))
    db = app_module.db
    db.upsert_seen(120, "pve1", "stopped-ct", "stopped", "")
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode(), **H}
    post = lambda body: client.post("/api/containers/120/snapshots", json=body, headers=auth)
    r = post({"name": "manual-1", "description": "test"})
    assert r.status_code == 200 and len(started) == 1  # stopped containers can be snapshotted too
    job = db.job(r.json()["job"])
    assert job["kind"] == "snapshot" and job["snapshot"] == "manual-1"
    app_module.jobs.active.clear()
    assert post({"name": "lxcwatch-mine"}).status_code == 409  # reserved for automatic snapshots
    assert post({"name": "9bad"}).status_code == 409
    assert post({"name": "x" * 41}).status_code == 409
    assert post({"name": "ok", "description": "d" * 201}).status_code == 409
    assert post({"name": "manual-2"}).status_code == 200
    assert post({"name": "manual-3"}).status_code == 409  # a job is already running on it


# Cloudflare Access

TEAM, AUD = "myteam", "aud-tag"
ISSUER = f"https://{TEAM}.cloudflareaccess.com"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key, kid="k1"):
    return {"keys": [{**RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "use": "sig", "alg": "RS256"}]}


def token(key, kid="k1", **claims):
    body = {"aud": [AUD], "iss": ISSUER, "email": "me@example.com", "exp": int(time.time()) + 600, **claims}
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def access_client(cfg, monkeypatch, rsa_key):
    from fastapi.testclient import TestClient
    import lxcwatch.app as app_module
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_ENABLED", "true")
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_TEAM", TEAM)
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_EMAILS", "Me@Example.com, other@example.com")
    app_module = importlib.reload(app_module)
    async def fetch():
        return jwks(rsa_key)
    monkeypatch.setattr(app_module.auth, "_fetch_access_jwks", fetch)
    return TestClient(app_module.app, base_url="http://testserver")


def access(tok):
    return {"Cf-Access-Jwt-Assertion": tok}


def test_access_token_signs_in(access_client, rsa_key):
    h = access(token(rsa_key))
    assert access_client.get("/api/containers", headers=h).status_code == 200
    assert access_client.get("/", headers=h, follow_redirects=False).status_code == 200
    assert access_client.get("/login", headers=h, follow_redirects=False).status_code == 303
    assert access_client.get("/api/containers", headers=access(token(rsa_key, email="OTHER@example.com"))).status_code == 200
    assert "set-cookie" not in access_client.get("/api/containers", headers=h).headers
    assert access_client.get("/api/containers").status_code == 401  # the header is what signs in


def test_page_is_told_how_you_signed_in(access_client, rsa_key):
    good = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    who = lambda **h: access_client.get("/api/containers", headers=h).json()["access_email"]
    assert who(**access(token(rsa_key, email="ME@example.com"))) == "me@example.com"
    assert who(Authorization=good) is None
    assert who(Authorization=good, **access(token(rsa_key, email="mallory@example.com"))) is None
    assert access_client.post("/api/login", json={"username": "admin", "password": "s3cret"}, headers=H).status_code == 200
    assert who() is None
    assert who(**access(token(rsa_key))) == "me@example.com"  # both: Access is what shows


def test_access_rejects_bad_tokens(access_client, rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    public_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    bad = {
        "not on the allowlist": token(rsa_key, email="mallory@example.com"),
        "service token, no email": token(rsa_key, email=None, common_name="abc.access"),
        "another application": token(rsa_key, aud=["other-aud"]),
        "no audience": token(rsa_key, aud=None),
        "another team": token(rsa_key, iss="https://evil.cloudflareaccess.com"),
        "no issuer": token(rsa_key, iss=None),
        "expired": token(rsa_key, exp=now - 3600),
        "no expiry": token(rsa_key, exp=None),
        "not valid yet": token(rsa_key, nbf=now + 3600),
        "forged signature": token(other),
        "unknown key": token(other, kid="k2"),
        "unsigned": jwt.encode({"aud": [AUD], "iss": ISSUER, "email": "me@example.com", "exp": now + 600},
                               None, algorithm="none", headers={"kid": "k1"}),
        "garbage": "not.a.jwt",
        "tampered": token(rsa_key)[:-4] + "AAAA",
    }
    enc = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")
    signing = enc(json.dumps({"alg": "HS256", "kid": "k1"}).encode()) + b"." + enc(json.dumps(
        {"aud": [AUD], "iss": ISSUER, "email": "me@example.com", "exp": now + 600}).encode())
    # The classic algorithm confusion: HMAC keyed with the public key.
    bad["hmac with the public key"] = (signing + b"." + enc(hmac.new(public_pem, signing, hashlib.sha256).digest())).decode()
    for why, tok in bad.items():
        assert access_client.get("/api/containers", headers=access(tok)).status_code == 401, why
    r = access_client.get("/", headers=access(bad["forged signature"]), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_access_ignored_unless_configured(client, rsa_key):
    assert client.get("/api/containers", headers=access(token(rsa_key))).status_code == 401


def test_access_off_ignores_its_settings(access_client, rsa_key, monkeypatch):
    from fastapi.testclient import TestClient
    import lxcwatch.app as app_module
    h = access(token(rsa_key))
    assert access_client.get("/api/containers", headers=h).status_code == 200
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_ENABLED", "false")
    app_module = importlib.reload(app_module)
    fetches = []
    async def fetch():
        fetches.append(1)
        return jwks(rsa_key)
    monkeypatch.setattr(app_module.auth, "_fetch_access_jwks", fetch)
    off = TestClient(app_module.app, base_url="http://testserver")
    assert off.get("/api/containers", headers=h).status_code == 401
    assert off.get("/login", headers=h, follow_redirects=False).status_code == 200
    assert not fetches  # no calls out to Cloudflare either


def test_access_requests_still_need_the_csrf_header(access_client, rsa_key):
    h = access(token(rsa_key))
    assert access_client.post("/api/scan", headers=h).status_code == 403


def test_sign_out_leaves_the_access_session_alone(access_client, rsa_key):
    r = access_client.post("/api/logout", headers={**access(token(rsa_key)), **H})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_password_still_works_with_access_configured(access_client):
    r = access_client.post("/api/login", json={"username": "admin", "password": "s3cret"}, headers=H)
    assert r.status_code == 200
    assert access_client.get("/api/containers").status_code == 200


def test_access_key_rotation_and_fetch_limits(cfg, rsa_key, monkeypatch):
    access_cfg = dataclasses.replace(cfg, cf_access_team=TEAM, cf_access_aud=AUD, cf_access_emails=frozenset({"me@example.com"}))
    auth = Auth(access_cfg)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fetches, served = [], [jwks(rsa_key)]
    async def fetch():
        fetches.append(1)
        if isinstance(served[0], Exception):
            raise served[0]
        return served[0]
    monkeypatch.setattr(auth, "_fetch_access_jwks", fetch)
    check = lambda tok: asyncio.run(auth.check_access(tok))

    assert check(token(rsa_key)) and check(token(rsa_key)) and len(fetches) == 1

    # Unknown keys can't drive a fetch per request.
    assert not check(token(new_key, kid="k2")) and not check(token(new_key, kid="k3")) and len(fetches) == 1

    # A rotated key is picked up once the retry window passes, and the old one is dropped.
    served[0] = jwks(new_key, kid="k2")
    auth.access_keys_checked -= ACCESS_UNKNOWN_KEY_RETRY
    assert check(token(new_key, kid="k2")) and len(fetches) == 2
    assert not check(token(rsa_key))

    # An outage keeps the known keys in use.
    served[0] = httpx.ConnectError("down")
    auth.access_keys_checked -= ACCESS_KEYS_MAX_AGE
    assert check(token(new_key, kid="k2")) and len(fetches) == 3


def test_access_config(cfg, monkeypatch):
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_TEAM", "https://MyTeam.cloudflareaccess.com/")
    assert config.load().cf_access_team is None  # off unless enabled, whatever else is set
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_ENABLED", "true")
    with pytest.raises(SystemExit):  # enabling needs all three
        config.load()
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_AUD", AUD)
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_EMAILS", " A@example.com ,b@example.com,")
    loaded = config.load()
    assert loaded.cf_access_team == "myteam"
    assert loaded.cf_access_emails == {"a@example.com", "b@example.com"}
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_TEAM", "evil.example.com/x")
    with pytest.raises(SystemExit):
        config.load()
    monkeypatch.setenv("LXCWATCH_CF_ACCESS_ENABLED", "false")
    assert config.load().cf_access_team is None  # a bad value can't stop startup while it's off
