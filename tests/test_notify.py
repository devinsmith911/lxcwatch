import asyncio
import urllib.parse

import httpx
import pytest

from lxcwatch import config, notify
from lxcwatch.db import DB


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("LXCWATCH_PVE_HOST", "pve1")
    monkeypatch.setenv("LXCWATCH_PASSWORD", "x")
    monkeypatch.setenv("LXCWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LXCWATCH_PUSHOVER_TOKEN", "apptoken")
    monkeypatch.setenv("LXCWATCH_PUSHOVER_USER", "userkey")
    monkeypatch.setenv("LXCWATCH_PUBLIC_URL", "https://lxcwatch.example")
    return config.load()


def add_ct(db, vmid, outdated=True, security=0):
    db.upsert_seen(vmid, "pve1", f"ct{vmid}", "running", "")
    comps = [{"name": "app", "installed": "1.0", "target": "2.0", "state": "outdated",
              "release_url": f"https://github.com/x/app/releases/tag/v2.0-{vmid}"}] if outdated else []
    pkgs = [{"name": f"lib{i}", "from": "1", "to": "2", "security": True} for i in range(security)]
    db.save_scan(vmid, {"components": comps, "packages": pkgs}, None)


def send(cfg, db):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"status": 1})
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await notify.send(cfg, db, http)
    asyncio.run(go())
    return sent


def test_pushover_sends_once(cfg):
    db = DB(cfg.data_dir)
    add_ct(db, 101, outdated=True, security=2)
    [req] = send(cfg, db)
    assert str(req.url) == notify.PUSHOVER_URL
    form = dict(urllib.parse.parse_qsl(req.content.decode()))
    assert form["token"] == "apptoken" and form["user"] == "userkey"
    assert form["title"] == "2 LXC updates available"
    assert "101 ct101: app 1.0 → 2.0" in form["message"] and "2 security updates" in form["message"]
    assert form["url"] == "https://lxcwatch.example" and form["url_title"] == "Open lxcwatch"
    assert send(cfg, db) == []  # already announced


def test_pushover_message_fits_limit():
    lines = [{"text": f"{i} ct{i}: someapp 1.0.{i} → 2.0.{i}", "url": f"https://github.com/o/r/releases/tag/v2.0.{i}"}
             for i in range(60)]
    msg = notify.pushover_message(lines)
    assert len(msg) <= notify.PUSHOVER_MAX
    shown = msg.count("→")
    assert 0 < shown < 60 and msg.endswith(f"…and {60 - shown} more")
    assert notify.pushover_message(lines[:2]).count("→") == 2  # short lists are untouched


def test_pushover_needs_both_settings(cfg, monkeypatch):
    monkeypatch.delenv("LXCWATCH_PUSHOVER_USER")
    with pytest.raises(SystemExit, match="both"):
        config.load()
