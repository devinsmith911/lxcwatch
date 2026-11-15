import importlib
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from lxcwatch import config
from lxcwatch.shell import same_origin

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LXCWATCH_PVE_HOST", "192.0.2.1")
    monkeypatch.setenv("LXCWATCH_PASSWORD", "s3cret")
    monkeypatch.setenv("LXCWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LXCWATCH_SCAN_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("LXCWATCH_SCAN_ON_STARTUP", "never")
    monkeypatch.setenv("LXCWATCH_UPDATE_CHECK", "false")
    return monkeypatch


def make_app(monkeypatch, fake_cmd: str | None = None):
    from fastapi.testclient import TestClient
    import lxcwatch.app as app_module
    app_module = importlib.reload(app_module)
    app_module.db.upsert_seen(101, "pve1", "kuma", "running", "")
    app_module.db.upsert_seen(102, "pve1", "off", "stopped", "")

    async def resolve(node):
        return "192.0.2.1"
    monkeypatch.setattr(app_module.cluster, "resolve", resolve)
    seen = []

    def shell_argv(host, command):
        seen.append((host, command))
        return ["sh", "-c", fake_cmd or "exit 0"]
    monkeypatch.setattr(app_module.ssh, "shell_argv", shell_argv)
    client = TestClient(app_module.app, base_url="http://testserver")
    return client, app_module, seen


def sign_in(client):
    r = client.post("/api/login", json={"username": "admin", "password": "s3cret"}, headers={"X-Requested-With": "lxcwatch"})
    assert r.status_code == 200


def test_same_origin():
    assert same_origin("http://lxc.lan:8080", "lxc.lan:8080", None)
    assert same_origin("https://LXC.example.com", "lxc.example.com", None)
    assert not same_origin("https://evil.example", "lxc.lan:8080", None)
    assert not same_origin(None, "lxc.lan:8080", None)
    # A proxy that rewrites Host: the public URL still counts.
    assert same_origin("https://lxc.example.com", "10.0.0.5:8080", "https://lxc.example.com/")
    assert not same_origin("https://evil.example", "10.0.0.5:8080", "https://lxc.example.com")


def test_shell_refused_without_sign_in_or_from_another_site(env):
    client, _, seen = make_app(env)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/containers/101/shell", headers=ORIGIN):
            pass
    sign_in(client)
    for headers in ({}, {"origin": "https://evil.example"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/containers/101/shell", headers=headers):
                pass
    assert not seen


def test_shell_can_be_turned_off(env):
    env.setenv("LXCWATCH_SHELL", "false")
    client, _, seen = make_app(env)
    sign_in(client)
    assert client.get("/api/containers").json()["shell"] is False
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/containers/101/shell", headers=ORIGIN):
            pass
    assert not seen


def test_shell_needs_a_running_container(env):
    client, _, seen = make_app(env)
    sign_in(client)
    with client.websocket_connect("/api/containers/102/shell", headers=ORIGIN) as ws:
        assert "stopped" in json.loads(ws.receive_text())["error"]
    with client.websocket_connect("/api/containers/999/shell", headers=ORIGIN) as ws:
        assert json.loads(ws.receive_text())["error"] == "container not found"
    assert not seen


def test_shell_session(env):
    # Stands in for `ssh -tt node pct enter 101`: a program on a real pty.
    cmd = 'stty size; stty -echo; read line; echo "got:$line"; sleep 0.3; stty size; exit 3'
    client, _, seen = make_app(env, cmd)
    sign_in(client)
    out = b""
    with client.websocket_connect("/api/containers/101/shell?cols=100&rows=30", headers=ORIGIN) as ws:
        while b"30 100" not in out:
            out += ws.receive_bytes()
        ws.send_text(json.dumps({"resize": [120, 40]}))
        ws.send_bytes(b"hello\r")
        while True:
            msg = ws.receive()
            if msg.get("text"):
                assert json.loads(msg["text"]) == {"exit": 3}
                break
            out += msg["bytes"]
    assert seen == [("192.0.2.1", "pct enter 101")]
    assert b"got:hello" in out and b"40 120" in out


def test_shell_argv(env):
    from lxcwatch.ssh import SSH
    ssh = SSH(config.load())
    argv = ssh.shell_argv("10.0.0.11", "pct enter 101")
    assert argv[:2] == ["ssh", "-tt"] and "ControlMaster=no" in argv and argv[-1] == "pct enter 101"
    env.setenv("LXCWATCH_SSH_USER", "lxcwatch")
    assert SSH(config.load()).shell_argv("h", "pct enter 101")[-1] == "sudo -n sh -c 'pct enter 101'"
