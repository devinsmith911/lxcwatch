import asyncio
import json
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


def test_version_tuple():
    from lxcwatch.upstream import version_tuple
    assert version_tuple("1.2.3") == (1, 2, 3)
    assert version_tuple("v1.10.0") == (1, 10, 0)
    assert version_tuple("1.0.0+dev3.gabc") == (1, 0, 0)
    assert version_tuple("dev") is None and version_tuple(None) is None and version_tuple("1.2") is None
    assert version_tuple("1.10.0") > version_tuple("1.9.9")


def test_fresh_scan_bypasses_older_cache_once(cfg):
    import time
    from lxcwatch.upstream import FRESH_SINCE, Upstream
    up = Upstream(cfg)
    up._put(("release", "github", "a/b", None), "v1.0")
    assert up._get(("release", "github", "a/b", None), 3600) == (True, "v1.0")
    token = FRESH_SINCE.set(time.time() + 1)  # a manual scan that started after the entry was cached
    try:
        assert up._get(("release", "github", "a/b", None), 3600) == (False, None)
        up._cache[("release", "github", "a/b", None)] = (time.time() + 2, "v1.1")  # refetched during this scan
        assert up._get(("release", "github", "a/b", None), 3600) == (True, "v1.1")  # reused by other containers
    finally:
        FRESH_SINCE.reset(token)
    asyncio.run(up.close())


def test_own_update_is_always_announced(cfg, monkeypatch):
    import dataclasses
    from lxcwatch import cluster as cluster_mod
    cfg = dataclasses.replace(cfg, notify_packages="none")
    db = DB(cfg.data_dir)

    class FakeUpstream:
        async def lxcwatch_latest(self):
            return {"version": "1.1.0", "url": "https://git.example/releases/tag/1.1.0"}
    cl = cluster_mod.Cluster.__new__(cluster_mod.Cluster)
    cl.db, cl.upstream = db, FakeUpstream()
    monkeypatch.setattr(cluster_mod, "__version__", "1.0.0")
    asyncio.run(cl.check_self_update())
    [req] = send(cfg, db)
    form = dict(urllib.parse.parse_qsl(req.content.decode()))
    assert "lxcwatch 1.1.0 is available (you have 1.0.0)" in form["message"]
    assert "https://git.example/releases/tag/1.1.0" in form["message"]
    assert send(cfg, db) == []  # once per version
    monkeypatch.setattr(cluster_mod, "__version__", "1.1.0")
    asyncio.run(cl.check_self_update())
    assert db.get("self_update") == "null"  # up to date: nothing to show
    monkeypatch.setattr(cluster_mod, "__version__", "dev")
    asyncio.run(cl.check_self_update())
    assert db.get("self_update") == "null"  # source checkouts never nag


def plain_cfg(tmp_path, monkeypatch):
    for k in ("PUSHOVER_TOKEN", "PUSHOVER_USER", "PUBLIC_URL"):
        monkeypatch.delenv(f"LXCWATCH_{k}", raising=False)
    monkeypatch.setenv("LXCWATCH_PVE_HOST", "pve1")
    monkeypatch.setenv("LXCWATCH_PASSWORD", "x")
    monkeypatch.setenv("LXCWATCH_DATA_DIR", str(tmp_path))
    return config.load()


def test_slack_from_ui_settings(tmp_path, monkeypatch):
    cfg = plain_cfg(tmp_path, monkeypatch)
    db = DB(cfg.data_dir)
    notify.save_settings(cfg, db, {"slack_url": " https://hooks.slack.com/services/T/B/secret ", "public_url": "https://lxc.example"})
    add_ct(db, 101)
    db.upsert_seen(102, "pve1", "a<b>&c", "running", "")
    db.save_scan(102, {"components": [], "packages": [{"name": "x", "from": "1", "to": "2", "security": True}]}, None)
    [req] = send(cfg, db)
    assert str(req.url) == "https://hooks.slack.com/services/T/B/secret"
    text = json.loads(req.content)["text"]
    assert text.startswith("*2 LXC updates available*")
    assert "• 101 ct101: app 1.0 → 2.0 (<https://github.com/x/app/releases/tag/v2.0-101|release notes>)" in text
    assert "a&lt;b&gt;&amp;c: 1 security update" in text
    assert text.endswith("<https://lxc.example|Open lxcwatch>")


def test_slack_message_is_capped():
    lines = [{"text": f"line {i}", "url": None} for i in range(50)]
    text = notify.slack_payload("t", lines, None)["text"]
    assert text.count("• ") == notify.SLACK_MAX_LINES and text.endswith("…and 10 more")


def test_secrets_stay_server_side(tmp_path, monkeypatch):
    cfg = plain_cfg(tmp_path, monkeypatch)
    db = DB(cfg.data_dir)
    notify.save_settings(cfg, db, {"ntfy_url": "https://ntfy.sh/t", "ntfy_token": "tk", "packages": "all"})
    view = notify.public_settings(cfg, db)
    assert view["values"]["ntfy_token"] == "" and view["secrets_set"]["ntfy_token"] is True
    assert view["values"]["ntfy_url"] == "https://ntfy.sh/t" and view["values"]["packages"] == "all"
    # Saving without the secret keeps it; an empty string clears it.
    notify.save_settings(cfg, db, {"ntfy_url": "https://ntfy.sh/u", "ntfy_token": None})
    assert notify.load_settings(cfg, db)["ntfy_token"] == "tk"
    notify.save_settings(cfg, db, {"ntfy_token": ""})
    assert notify.load_settings(cfg, db)["ntfy_token"] == ""


def test_environment_wins_and_locks(cfg):
    db = DB(cfg.data_dir)
    notify.save_settings(cfg, db, {"pushover_token": "ui", "pushover_user": "ui", "packages": "none"})
    s = notify.load_settings(cfg, db)
    assert s["pushover_token"] == "apptoken" and s["packages"] == "none"
    assert notify.public_settings(cfg, db)["locked"]["pushover_token"] == "LXCWATCH_PUSHOVER_TOKEN"


def test_settings_are_checked(tmp_path, monkeypatch):
    cfg = plain_cfg(tmp_path, monkeypatch)
    db = DB(cfg.data_dir)
    with pytest.raises(ValueError, match="Slack webhook URL"):
        notify.save_settings(cfg, db, {"slack_url": "hooks.slack.com/x"})
    with pytest.raises(ValueError, match="Pushover needs both"):
        notify.save_settings(cfg, db, {"pushover_token": "t"})
    with pytest.raises(ValueError, match="none, security or all"):
        notify.save_settings(cfg, db, {"packages": "some"})


def test_send_test_reports_each_channel(tmp_path, monkeypatch):
    cfg = plain_cfg(tmp_path, monkeypatch)
    db = DB(cfg.data_dir)

    def handler(request):
        if "slack" in str(request.url):
            return httpx.Response(404, text="no_service")
        if "moved" in str(request.url):
            return httpx.Response(302, headers={"Location": "https://docs.example/"})
        return httpx.Response(200)

    async def go(data):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await notify.send_test(cfg, db, http, data)
    with pytest.raises(ValueError, match="at least one channel"):
        asyncio.run(go({}))
    results = asyncio.run(go({"slack_url": "https://hooks.slack.com/services/x", "webhook_url": "https://hook.example/x"}))
    assert results == [{"channel": "Slack", "ok": False, "error": "HTTP 404: no_service"},
                       {"channel": "webhook", "ok": True, "error": None}]
    assert notify.load_settings(cfg, db)["slack_url"] == ""  # testing doesn't save
    # A redirect is a failure, not a GET of wherever it points.
    [r] = asyncio.run(go({"webhook_url": "https://moved.example/x"}))
    assert not r["ok"] and "redirected" in r["error"]
