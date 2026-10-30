import asyncio

from lxcwatch.batch import Batches


class FakeDB:
    def __init__(self, cts):
        self.cts = {c["vmid"]: c for c in cts}

    def containers(self):
        return list(self.cts.values())

    def container(self, vmid):
        return self.cts.get(vmid)


class FakeJobs:
    """Records the order jobs run in and how many ran at once."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.order, self.calls, self.active, self.max_active = [], [], 0, 0

    def busy(self, vmid):
        return False

    def start(self, vmid, kind, snapshot, restart="never", packages=None, auto=False):
        self.order.append((vmid, kind))
        self.calls.append({"vmid": vmid, "kind": kind, "snapshot": snapshot, "restart": restart, "packages": packages, "auto": auto})
        return f"{vmid}-{kind}"

    async def wait(self, job_id):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        vmid, kind = job_id.split("-")
        failed = (int(vmid), kind) in self.fail
        return {"status": "failed" if failed else "success", "rc": 1 if failed else 0}


def ct(vmid, status="running", script="app", pkgmgr="apt"):
    return {"vmid": vmid, "name": f"ct{vmid}", "status": status, "info": {"script": script, "pkgmgr": pkgmgr}}


def run(batches, *args, **kw):
    async def go():
        b = batches.start(*args, **kw)
        await batches.task
        return b
    return asyncio.run(go())


def test_runs_one_at_a_time_app_before_os():
    jobs = FakeJobs()
    b = run(Batches(FakeDB([ct(101), ct(102)]), jobs), [102, 101], ["os", "app"], False, "auto", False)
    assert jobs.order == [(102, "app"), (102, "os"), (101, "app"), (101, "os")]
    assert jobs.max_active == 1
    assert b["status"] == "done" and all(s["status"] == "success" for s in b["steps"])


def test_only_limits_kinds_per_container():
    jobs = FakeJobs()
    run(Batches(FakeDB([ct(101), ct(102)]), jobs), [101, 102], ["app", "os"], False, "auto", False,
        only={101: ["app"], 102: ["os"]})
    assert jobs.order == [(101, "app"), (102, "os")]


def test_skips_what_cannot_run():
    jobs = FakeJobs()
    b = run(Batches(FakeDB([ct(101, status="stopped"), ct(102, script=None), ct(103)]), jobs),
            [101, 102, 103], ["app"], False, "auto", False)
    assert jobs.order == [(103, "app")]
    assert [s["status"] for s in b["steps"]] == ["skipped", "skipped", "success"]
    assert b["steps"][0]["note"] == "container is stopped"


def test_failures_continue_or_stop():
    jobs = FakeJobs(fail={(101, "app")})
    b = run(Batches(FakeDB([ct(101), ct(102)]), jobs), [101, 102], ["app"], False, "auto", False)
    assert [s["status"] for s in b["steps"]] == ["failed", "success"] and b["status"] == "done"

    jobs = FakeJobs(fail={(101, "app")})
    b = run(Batches(FakeDB([ct(101), ct(102)]), jobs), [101, 102], ["app"], False, "auto", True)
    assert jobs.order == [(101, "app")]
    assert [s["status"] for s in b["steps"]] == ["failed", "skipped"] and b["status"] == "stopped"


def test_stop_finishes_current_then_skips_rest():
    async def go():
        jobs = FakeJobs()
        batches = Batches(FakeDB([ct(101), ct(102), ct(103)]), jobs)
        b = batches.start([101, 102, 103], ["app"], False, "auto", False)
        await asyncio.sleep(0.005)  # first job is running
        batches.stop()
        await batches.task
        return b, jobs
    b, jobs = asyncio.run(go())
    assert jobs.order == [(101, "app")]
    assert [s["status"] for s in b["steps"]] == ["success", "skipped", "skipped"] and b["status"] == "stopped"


def test_only_one_batch_at_a_time():
    async def go():
        batches = Batches(FakeDB([ct(101)]), FakeJobs())
        batches.start([101], ["app"], False, "auto", False)
        try:
            batches.start([101], ["app"], False, "auto", False)
        except Exception as e:
            return str(e)
        finally:
            await batches.task
    assert "already in progress" in asyncio.run(go())


def test_db_migration_adds_started_at(tmp_path):
    import sqlite3
    from lxcwatch.db import DB
    old = sqlite3.connect(tmp_path / "lxcwatch.db")
    old.execute("CREATE TABLE containers (vmid INTEGER PRIMARY KEY, node TEXT NOT NULL, name TEXT, status TEXT, "
                "tags TEXT, info TEXT, error TEXT, scanned_at REAL, seen_at REAL)")
    old.execute("INSERT INTO containers (vmid, node, name, status) VALUES (101, 'pve1', 'old', 'running')")
    old.commit(); old.close()
    db = DB(str(tmp_path))
    assert db.container(101)["started_at"] is None  # existing rows survive
    db.upsert_seen(101, "pve1", "old", "running", "", 1000.0)
    assert db.container(101)["started_at"] == 1000.0


def test_discover_records_start_time(tmp_path, monkeypatch):
    import time
    from lxcwatch.cluster import Cluster
    from lxcwatch.db import DB
    monkeypatch.setattr(time, "time", lambda: 10_000.0)
    db = DB(str(tmp_path))
    cl = Cluster.__new__(Cluster)
    cl.db, cl.cfg, cl.nodes = db, type("C", (), {"pve_host": "h", "node_addresses": {}})(), {}

    async def pvesh(path, host=None, timeout=60):
        if path == "/cluster/status":
            return [{"type": "node", "name": "pve1", "ip": "10.0.0.1"}]
        return [{"type": "lxc", "vmid": 101, "node": "pve1", "name": "up", "status": "running", "uptime": 3600},
                {"type": "lxc", "vmid": 102, "node": "pve1", "name": "down", "status": "stopped", "uptime": 0}]
    cl.pvesh = pvesh
    asyncio.run(cl.discover())
    assert db.container(101)["started_at"] == 10_000.0 - 3600
    assert db.container(102)["started_at"] is None
