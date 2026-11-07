"""Automatic updates: install opted-in containers' updates in a maintenance window.

Settings (the window, days, minimum release age) live in the kv table and
are edited in the Automatic updates dialog; each container's policy lives in
auto_policy. When a window opens, lxcwatch scans fresh, plans the work and
runs it through the Update all runner, one step at a time. A failed step
pauses automatic updates for that container until a manual update succeeds
or the user resumes it.
"""

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

from . import notify
from .jobs import AUTO_SNAPSHOT_PREFIX

log = logging.getLogger(__name__)

POLICIES = ("off", "security", "packages", "app")
DEFAULTS = {"enabled": False, "start": "03:00", "end": "05:00", "days": [0, 1, 2, 3, 4, 5, 6],
            "min_age_days": 3, "snapshot_packages": True}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_AGE_DAYS = 60


def load_settings(db) -> dict:
    saved = json.loads(db.get("auto_settings") or "{}")
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def validate_settings(data: dict) -> dict:
    s = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    for key in ("start", "end"):
        if not isinstance(s[key], str) or not TIME_RE.match(s[key]):
            raise ValueError(f"{key} must be a time like 03:00")
    if s["start"] == s["end"]:
        raise ValueError("the window needs different start and end times")
    if not isinstance(s["days"], list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in s["days"]):
        raise ValueError("days must be weekday numbers, 0 (Monday) to 6 (Sunday)")
    s["days"] = sorted(set(s["days"]))
    if s["enabled"] and not s["days"]:
        raise ValueError("pick at least one day")
    if not isinstance(s["min_age_days"], int) or not 0 <= s["min_age_days"] <= MAX_AGE_DAYS:
        raise ValueError(f"the minimum release age must be 0 to {MAX_AGE_DAYS} days")
    s["enabled"], s["snapshot_packages"] = bool(s["enabled"]), bool(s["snapshot_packages"])
    return s


def _at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, dtime(h, m))


def _span(day: date, s: dict) -> tuple[datetime, datetime]:
    start, end = _at(day, s["start"]), _at(day, s["end"])
    return start, end if end > start else end + timedelta(days=1)  # overnight windows end the next day


def window_at(now: float, s: dict) -> tuple[float, float] | None:
    """(start, end) of the window that `now` falls in, in server local time; None outside one."""
    here = datetime.fromtimestamp(now)
    for back in (0, 1):  # an overnight window may have started yesterday
        day = here.date() - timedelta(days=back)
        if day.weekday() in s["days"]:
            start, end = _span(day, s)
            if start <= here < end:
                return start.timestamp(), end.timestamp()
    return None


def next_window(now: float, s: dict) -> float | None:
    """When the current or next window starts (None if automatic updates are off)."""
    if not s["enabled"] or not s["days"]:
        return None
    current = window_at(now, s)
    if current:
        return current[0]
    here = datetime.fromtimestamp(now)
    for ahead in range(8):
        day = here.date() + timedelta(days=ahead)
        start = _at(day, s["start"])
        if day.weekday() in s["days"] and start > here:
            return start.timestamp()
    return None


def _ts(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def release_age(comp: dict, now: float) -> float:
    """Seconds since the target version was published (or first seen by lxcwatch)."""
    since = _ts(comp.get("published")) or _ts(comp.get("first_seen")) or now
    return now - since


def is_paused(policy: dict | None, last_success: float) -> bool:
    """Paused by a failure, and no update has succeeded since."""
    return bool(policy and policy.get("paused_at") and last_success <= policy["paused_at"])


def plan(containers: list[dict], policies: dict[int, dict], settings: dict, now: float,
         last_success: dict[int, float]) -> tuple[list[dict], list[dict]]:
    """Steps to run and containers skipped (with why), for opted-in containers.

    `containers` are the dashboard summaries (app._summary), so failures are
    already marked superseded and stale package lists flagged.
    """
    steps: list[dict] = []
    later: list[dict] = []  # lxcwatch's own container goes last
    skipped: list[dict] = []
    min_age = settings["min_age_days"] * 86400
    for c in containers:
        pol = policies.get(c["vmid"]) or {}
        policy = pol.get("policy", "off")
        if policy == "off":
            continue

        def skip(reason: str) -> None:
            skipped.append({"vmid": c["vmid"], "name": c["name"], "reason": reason})

        if is_paused(pol, last_success.get(c["vmid"], 0.0)):
            skip(f"paused: {pol.get('paused_reason') or 'an automatic update failed'}")
            continue
        if c["status"] != "running":
            skip(f"container is {c['status']}")
            continue
        if c.get("job"):
            skip("another job is running")
            continue
        if any(x.get("pending") for x in c["components"]):
            skip("the last app update did not finish")
            continue
        if any(not f.get("superseded") for f in c.get("failures", [])):
            skip("a community-script failure needs a look")
            continue
        state = c.get("script_state") or {}
        if state.get("state") == "deleted" or (state.get("found") is False and state.get("message")):
            skip("its community-script was retired")
            continue
        if c.get("pkg_stale"):
            skip("package lists couldn't be refreshed")
            continue
        own = bool(c.get("lxcwatch_host"))
        mine: list[dict] = []
        if policy == "app" and c.get("script"):
            outdated = [x for x in c["components"] if x["state"] == "outdated"]
            young = [x for x in outdated if release_age(x, now) < min_age]
            if young:
                x = young[0]
                ready = datetime.fromtimestamp(now + min_age - release_age(x, now)).strftime("%d %b")
                skip(f"waiting for {x['name']} {x['target']} to be {settings['min_age_days']} days old (from {ready})")
            elif outdated:
                mine.append({"vmid": c["vmid"], "name": c["name"], "kind": "app", "snapshot": True})
        pkgs = c.get("packages", [])
        if policy == "security":
            pkgs = [p for p in pkgs if p["security"]]
        names = [p["name"] for p in pkgs if not (own and p["name"] == "lxcwatch")]
        if names:
            explicit = policy == "security" or own and len(names) < len(pkgs)
            mine.append({"vmid": c["vmid"], "name": c["name"], "kind": "os",
                         "snapshot": settings["snapshot_packages"],
                         # Restarting lxcwatch's own container would cut the run short.
                         "restart": "never" if own else "auto",
                         "packages": names if explicit else None})
        (later if own else steps).extend(mine)
    return steps + later, skipped


class AutoUpdater:
    def __init__(self, cfg, db, cluster, jobs, batches, http, summaries, scan):
        self.cfg, self.db, self.cluster, self.jobs, self.batches, self.http = cfg, db, cluster, jobs, batches, http
        self.summaries, self.scan = summaries, scan  # callables from app.py
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    def last_run(self) -> dict | None:
        return json.loads(self.db.get("auto_last_run") or "null")

    def paused(self, vmid: int, policy: dict | None) -> bool:
        return is_paused(policy, self.db.last_success(vmid))

    async def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        s = load_settings(self.db)
        window = window_at(now, s) if s["enabled"] else None
        if self.running:
            if not window and self.batches.running and (self.batches.current or {}).get("auto"):
                self.batches.stop("the maintenance window ended")
            return
        if not window:
            return
        key = str(int(window[0]))
        if self.db.get("auto_last_window") == key:
            return
        if self.batches.running or self.cluster.scan_lock.locked():
            return  # a manual run or scan is busy; try again next tick
        self.db.set("auto_last_window", key)
        self.task = asyncio.create_task(self._run(s))

    async def _run(self, s: dict) -> None:
        run = {"started_at": time.time(), "finished_at": None, "status": "running", "steps": [], "skipped": []}
        self.db.set("auto_last_run", json.dumps(run))
        try:
            await self.scan()
            policies = self.db.auto_policies()
            containers = self.summaries()
            last_ok = {c["vmid"]: self.db.last_success(c["vmid"]) for c in containers if c["vmid"] in policies}
            steps, run["skipped"] = plan(containers, policies, s, time.time(), last_ok)
            if steps:
                batch = self.batches.start_plan(steps, {"kinds": ["app", "os"]}, auto=True, after_step=self._after_step)
                await self.batches.task
                run["status"] = batch["status"]
            else:
                run["status"] = "done"
            run["steps"] = [{k: st.get(k) for k in ("vmid", "name", "kind", "status", "note", "job")} for st in steps]
        except Exception as e:
            log.exception("automatic update run failed")
            run["status"], run["error"] = "failed", str(e)
        run["finished_at"] = time.time()
        self.db.set("auto_last_run", json.dumps(run))
        await self._notify(run)

    async def _after_step(self, step: dict) -> None:
        if step["status"] != "failed":
            return
        what = "app" if step["kind"] == "app" else "package"
        job = self.db.job(step["job"]) if step.get("job") else None
        if job and job.get("rc") == 90:  # the job script's code for "snapshot failed, nothing changed"
            step["note"] = "the snapshot couldn't be taken; does this storage support snapshots?"
            self.db.pause_auto(step["vmid"], f"the snapshot before the automatic {what} update couldn't be taken")
            return
        why = f" ({step['note']})" if step.get("note") else ""
        self.db.pause_auto(step["vmid"], f"the automatic {what} update failed{why}")
        pol = self.db.auto_policies().get(step["vmid"]) or {}
        snap = (job or {}).get("snapshot") or ""
        if not (pol.get("rollback") and snap.startswith(AUTO_SNAPSHOT_PREFIX) and job.get("rc") != 90):
            return
        try:
            rb = self.jobs.start(step["vmid"], "rollback", False, target=snap, auto=True)
            result = await self.jobs.wait(rb)
            ok = result and result["status"] == "success"
            step["note"] = f"{step.get('note') or 'failed'}; {'rolled back to' if ok else 'rollback to'} {snap}{'' if ok else ' failed'}"
        except Exception as e:
            step["note"] = f"{step.get('note') or 'failed'}; rollback failed: {e}"

    async def _notify(self, run: dict) -> None:
        done = [s for s in run["steps"] if s["status"] == "success"]
        failed = [s for s in run["steps"] if s["status"] == "failed"]
        if not done and not failed and not run.get("error"):
            return  # nothing happened
        label = {"app": "app", "os": "packages"}
        lines = [{"text": f"{s['vmid']} {s['name']}: {label[s['kind']]} update failed" + (f" ({s['note']})" if s.get("note") else ""),
                  "url": None, "vmid": s["vmid"]} for s in failed]
        lines += [{"text": f"{s['vmid']} {s['name']}: {label[s['kind']]} updated", "url": None, "vmid": s["vmid"]} for s in done]
        if run.get("error"):
            lines.insert(0, {"text": f"The run stopped: {run['error']}", "url": None, "vmid": None})
        title = f"Automatic updates: {len(done)} updated" + (f", {len(failed)} failed" if failed else "")
        try:
            await notify.deliver(self.cfg, self.db, self.http, title, lines, tags="arrows_counterclockwise")
        except Exception:
            log.exception("automatic update notification failed")
