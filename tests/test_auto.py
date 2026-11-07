import asyncio
import json
import time
from datetime import datetime

import pytest

from lxcwatch import auto
from lxcwatch.batch import Batches
from lxcwatch.db import DB


def ts(text):
    return datetime.fromisoformat(text).timestamp()


def settings(**kw):
    return auto.validate_settings({**auto.DEFAULTS, "enabled": True, **kw})


def test_validate_settings():
    s = settings(start="23:30", end="01:00", days=[5, 6, 5], min_age_days=3)
    assert s["days"] == [5, 6] and s["min_age_days"] == 3
    for bad in ({"start": "25:00"}, {"start": "03:00", "end": "03:00"}, {"days": [7]}, {"days": []},
                {"min_age_days": -1}, {"min_age_days": "3"}):
        with pytest.raises(ValueError):
            settings(**bad)


def test_windows_same_day_and_overnight():
    s = settings(start="03:00", end="05:00", days=[0])  # Mondays
    assert auto.window_at(ts("2026-11-02T04:00"), s) == (ts("2026-11-02T03:00"), ts("2026-11-02T05:00"))
    assert auto.window_at(ts("2026-11-02T05:00"), s) is None
    assert auto.window_at(ts("2026-11-03T04:00"), s) is None  # Tuesday
    assert auto.next_window(ts("2026-11-02T06:00"), s) == ts("2026-11-09T03:00")
    night = settings(start="23:00", end="02:00", days=[4])  # starts Friday night
    assert auto.window_at(ts("2026-11-07T01:30"), night) == (ts("2026-11-06T23:00"), ts("2026-11-07T02:00"))
    assert auto.window_at(ts("2026-11-07T23:30"), night) is None  # Saturday isn't a start day
    assert auto.next_window(ts("2026-11-06T12:00"), night) == ts("2026-11-06T23:00")
    assert auto.next_window(time.time(), {**night, "enabled": False}) is None


NOW = ts("2026-11-02T03:00")


def ct(vmid, **kw):
    base = {"vmid": vmid, "name": f"ct{vmid}", "status": "running", "job": None, "script": "app", "components": [],
            "packages": [], "failures": [], "script_state": None, "pkg_stale": False, "lxcwatch_host": False}
    return {**base, **kw}


def comp(published_days_ago=None, first_seen_days_ago=None, state="outdated"):
    return {"name": "app", "target": "2.0", "state": state,
            "published": None if published_days_ago is None else datetime.fromtimestamp(NOW - published_days_ago * 86400).isoformat(),
            "first_seen": None if first_seen_days_ago is None else NOW - first_seen_days_ago * 86400}


PKGS = [{"name": "openssl", "from": "1", "to": "2", "security": True}, {"name": "curl", "from": "1", "to": "2", "security": False}]


def test_plan_policies_and_ordering():
    cts = [ct(100, lxcwatch_host=True, packages=PKGS + [{"name": "lxcwatch", "from": "1", "to": "2", "security": False}]),
           ct(101, components=[comp(published_days_ago=5)], packages=PKGS),
           ct(102, packages=PKGS),
           ct(103, packages=PKGS)]
    pol = {100: {"policy": "packages"}, 101: {"policy": "app"}, 102: {"policy": "security"}}
    steps, skipped = auto.plan(cts, pol, settings(), NOW, {})
    got = [(s["vmid"], s["kind"], s.get("packages"), s.get("restart")) for s in steps]
    assert got == [(101, "app", None, None), (101, "os", None, "auto"), (102, "os", ["openssl"], "auto"),
                   # lxcwatch's own container: last, never restarted, without its own package
                   (100, "os", ["openssl", "curl"], "never")]
    assert skipped == []


def test_plan_waits_for_new_releases_and_skips_unsafe():
    s = settings(min_age_days=3)
    cts = [ct(1, components=[comp(published_days_ago=1)]),
           ct(2, components=[comp(first_seen_days_ago=4)]),
           ct(3, status="stopped"),
           ct(4, pkg_stale=True, packages=PKGS),
           ct(5, failures=[{"superseded": False}]),
           ct(6, components=[{**comp(published_days_ago=9), "pending": True}]),
           ct(7, packages=PKGS)]
    pol = {i: {"policy": "app"} for i in range(1, 8)}
    pol[7] = {"policy": "packages", "paused_at": NOW - 100, "paused_reason": "the automatic package update failed"}
    steps, skipped = auto.plan(cts, pol, s, NOW, {7: NOW - 200})
    assert [(x["vmid"], x["kind"]) for x in steps] == [(2, "app")]
    reasons = {x["vmid"]: x["reason"] for x in skipped}
    assert reasons[1].startswith("waiting for app 2.0 to be 3 days old")
    assert reasons[3] == "container is stopped" and reasons[4] == "package lists couldn't be refreshed"
    assert "failure" in reasons[5] and "did not finish" in reasons[6] and reasons[7].startswith("paused")
    # A manual update that succeeded after the pause resumes it.
    steps, _ = auto.plan([ct(7, packages=PKGS)], pol, s, NOW, {7: NOW - 50})
    assert [x["vmid"] for x in steps] == [7]


class FakeJobs:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def busy(self, vmid):
        return False

    def start(self, vmid, kind, snapshot, target=None, restart="never", packages=None, auto=False):
        self.calls.append((vmid, kind, target, auto))
        return f"{vmid}-{kind}-{len(self.calls)}"

    async def wait(self, job_id):
        vmid, kind, _ = job_id.split("-")
        failed = (int(vmid), kind) in self.fail
        return {"status": "failed" if failed else "success", "rc": 1 if failed else 0}


class FakeCluster:
    class _L:
        def locked(self):
            return False
    scan_lock = _L()


def test_run_pauses_rolls_back_and_runs_once_per_window(tmp_path, monkeypatch):
    db = DB(str(tmp_path))
    for vmid in (101, 102):
        db.upsert_seen(vmid, "pve1", f"ct{vmid}", "running", "")
        db.save_scan(vmid, {"script": "app", "pkgmgr": "apt"}, None)
    db.set("auto_settings", json.dumps(settings(start="03:00", end="05:00")))
    db.set_auto_policy(101, "packages", rollback=True)
    db.set_auto_policy(102, "packages", rollback=False)
    jobs = FakeJobs(fail={(101, "os")})
    batches = Batches(db, jobs)
    sent = []

    async def deliver(cfg, db, http, title, lines, tags="package"):
        sent.append((title, [l["text"] for l in lines]))
        return True
    monkeypatch.setattr(auto.notify, "deliver", deliver)
    scans = []

    async def scan():
        scans.append(1)
    cts = [ct(101, packages=PKGS), ct(102, packages=PKGS)]
    up = auto.AutoUpdater(None, db, FakeCluster(), jobs, batches, None, lambda: cts, scan)
    # The failed job's snapshot, as Jobs would have recorded it.
    real_job = db.job
    db.job = lambda job_id: {"snapshot": "lxcwatch-20261102-030000", "rc": 1} if job_id.startswith("101-os") else real_job(job_id)

    async def go():
        await up.tick(ts("2026-11-02T03:01"))
        await up.task
        await up.tick(ts("2026-11-02T03:30"))  # same window: no second run
        assert not up.running
    asyncio.run(go())
    assert len(scans) == 1
    assert (101, "rollback", "lxcwatch-20261102-030000", True) in jobs.calls
    assert all(c[3] for c in jobs.calls)  # every job marked automatic
    run = up.last_run()
    assert [(s["vmid"], s["status"]) for s in run["steps"]] == [(101, "failed"), (102, "success")]
    assert "rolled back to lxcwatch-20261102-030000" in run["steps"][0]["note"]
    assert db.auto_policies()[101]["paused_at"]
    assert sent[0][0] == "Automatic updates: 1 updated, 1 failed"


def test_package_list_job_script():
    import subprocess
    from lxcwatch.jobs import JobError, node_script
    script = node_script("j9", 105, "os", None, None, restart="never", packages=["openssl", "libssl3"])
    assert "install --only-upgrade openssl libssl3" in script and "dist-upgrade" not in script
    subprocess.run(["bash", "-n"], input=script.encode(), check=True)


def test_window_end_stops_the_run(tmp_path):
    db = DB(str(tmp_path))
    db.set("auto_settings", json.dumps(settings(start="03:00", end="05:00")))

    class SlowJobs(FakeJobs):
        async def wait(self, job_id):
            await asyncio.sleep(0.05)
            return {"status": "success", "rc": 0}
    for vmid in (1, 2, 3):
        db.upsert_seen(vmid, "pve1", f"ct{vmid}", "running", "")
        db.save_scan(vmid, {"script": "app", "pkgmgr": "apt"}, None)
        db.set_auto_policy(vmid, "packages", False)
    batches = Batches(db, SlowJobs())

    async def scan():
        pass
    up = auto.AutoUpdater(None, db, FakeCluster(), batches.jobs, batches, None,
                          lambda: [ct(v, packages=PKGS) for v in (1, 2, 3)], scan)

    async def go():
        await up.tick(ts("2026-11-02T04:59"))
        await asyncio.sleep(0.02)
        await up.tick(ts("2026-11-02T05:00"))  # window over: finish the current job, skip the rest
        await up.task
    asyncio.run(go())
    steps = up.last_run()["steps"]
    assert steps[0]["status"] == "success"
    assert {s["note"] for s in steps[1:]} == {"the maintenance window ended"}
