"""Update and rollback jobs.

A job is a small bash script written to the Proxmox node and started with
setsid/nohup, logging to /var/log/lxcwatch/<id>.log. We follow that log over
SSH. Because the work runs detached on the node, restarting lxcwatch (or a
dropped SSH connection) never kills an update halfway through; on startup we
simply re-attach to whatever was still running.
"""

import asyncio
import logging
import re
import secrets
import shlex
import time

from .cluster import SNAPSHOT_NAME_RE, Cluster
from .config import Config
from .db import DB
from .ssh import SSH

log = logging.getLogger(__name__)

LOGDIR = "/var/log/lxcwatch"
MARK = "__LXCWATCH_EXIT__"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")
KINDS = ("app", "os", "rollback")

# Community-scripts' `update` runs its silent path when PHS_SILENT=1 (and
# whenever there is no TTY), picking the default answer for every menu.
APP_CMD = "env PHS_SILENT=1 TERM=dumb update"
OS_CMD = "sh -c " + shlex.quote(
    "if command -v apt-get >/dev/null 2>&1; then "
    "export DEBIAN_FRONTEND=noninteractive; apt-get update && "
    "apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade; "
    "elif command -v apk >/dev/null 2>&1; then apk -U upgrade; "
    "else echo 'no supported package manager'; exit 1; fi"
)


def clean_line(raw: bytes) -> str:
    text = ANSI_RE.sub("", raw.decode(errors="replace"))
    parts = [p for p in text.split("\r") if p.strip()]
    return parts[-1].rstrip() if parts else ""


def node_script(job_id: str, vmid: int, kind: str, snapshot: str | None, target: str | None) -> str:
    q = shlex.quote
    lines = [
        "#!/bin/bash",
        f"echo \"[lxcwatch] {kind} job {job_id} for CT {vmid} started $(date -Is)\"",
    ]
    if snapshot:
        lines += [
            f"echo '[lxcwatch] creating snapshot {snapshot}'",
            f"if ! pct snapshot {vmid} {snapshot} --description {q(f'lxcwatch: before {kind} update')}; then",
            "  echo '[lxcwatch] snapshot failed, nothing was changed'",
            f"  echo '{MARK} 90'",
            "  exit 0",
            "fi",
        ]
    if kind == "app":
        lines += [f"echo '[lxcwatch] running community-scripts update'", f"pct exec {vmid} -- {APP_CMD} </dev/null", "rc=$?"]
    elif kind == "os":
        lines += [f"echo '[lxcwatch] upgrading OS packages'", f"pct exec {vmid} -- {OS_CMD} </dev/null", "rc=$?"]
    elif kind == "rollback":
        lines += [
            f"was_running=$(pct status {vmid} | grep -q running && echo 1)",
            f"echo '[lxcwatch] rolling back to {target}'",
            f"pct rollback {vmid} {target}",
            "rc=$?",
            f"if [ $rc -eq 0 ] && [ \"$was_running\" = 1 ] && ! pct status {vmid} | grep -q running; then",
            "  echo '[lxcwatch] starting container'",
            f"  pct start {vmid}; rc=$?",
            "fi",
        ]
    lines.append(f"echo \"[lxcwatch] finished $(date -Is) with exit code $rc\"")
    lines.append(f"echo \"{MARK} $rc\"")
    return "\n".join(lines) + "\n"


class JobError(Exception):
    pass


class Jobs:
    def __init__(self, cfg: Config, ssh: SSH, db: DB, cluster: Cluster):
        self.cfg, self.ssh, self.db, self.cluster = cfg, ssh, db, cluster
        self.sem = asyncio.Semaphore(max(1, cfg.job_concurrency))
        self.active: dict[int, str] = {}
        self.subs: dict[str, set[asyncio.Queue]] = {}
        self.tasks: set[asyncio.Task] = set()
        cluster.is_busy = self.busy

    def busy(self, vmid: int) -> bool:
        return vmid in self.active

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def start(self, vmid: int, kind: str, snapshot: bool, target: str | None = None) -> str:
        if kind not in KINDS:
            raise JobError(f"unknown job kind {kind!r}")
        row = self.db.container(vmid)
        if not row:
            raise JobError(f"CT {vmid} not found")
        if self.busy(vmid):
            raise JobError(f"CT {vmid} already has a job running")
        if kind in ("app", "os") and row["status"] != "running":
            raise JobError(f"CT {vmid} is {row['status']}, start it first")
        if kind == "app" and not (row["info"] or {}).get("script"):
            raise JobError(f"CT {vmid} has no community-scripts update command")
        if kind == "rollback" and not (target and SNAPSHOT_NAME_RE.match(target)):
            raise JobError("rollback needs a valid snapshot name")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{vmid}-{secrets.token_hex(2)}"
        snap = f"lxcwatch-{stamp}" if snapshot and kind != "rollback" else None
        self.db.create_job(job_id, vmid, row["node"], kind, snap or target)
        self.active[vmid] = job_id
        self._spawn(self._run(job_id, vmid, row["node"], kind, snap, target))
        return job_id

    def resume(self) -> None:
        for job in self.db.unfinished_jobs():
            if job["status"] == "queued":
                self.db.update_job(job["id"], status="failed", finished_at=time.time())
                self.db.append_log(job["id"], "[lxcwatch] lxcwatch restarted before this job started\n")
                continue
            self.active[job["vmid"]] = job["id"]
            seen = len(job["log"].splitlines())
            self._spawn(self._resume(job, seen))

    async def _resume(self, job: dict, seen: int) -> None:
        try:
            addr = await self.cluster.resolve(job["node"])
            r = await self.ssh.run(addr, f"test -f {LOGDIR}/{job['id']}.log", timeout=30)
            if not r.ok:
                raise JobError("job log is gone from the node (rebooted?), result unknown")
            # Fails fast if the job died while we were away; else we follow it.
            await self._exit_code_if_dead(addr, job["id"])
            rc = await self._follow(job["id"], addr, seen)
            await self._finish(job["id"], job["vmid"], job["node"], job["kind"], job["snapshot"] if job["kind"] != "rollback" else None, rc)
        except Exception as e:
            self._fail(job["id"], job["vmid"], e)

    async def _run(self, job_id: str, vmid: int, node: str, kind: str, snap: str | None, target: str | None) -> None:
        try:
            async with self.sem:
                self.db.update_job(job_id, status="running")
                addr = await self.cluster.resolve(node)
                script = node_script(job_id, vmid, kind, snap, target)
                base = f"{LOGDIR}/{job_id}"
                r = await self.ssh.run(
                    addr,
                    # Braces matter: a trailing & would background the whole
                    # chain, and background jobs read stdin from /dev/null.
                    f"mkdir -p {LOGDIR} && cat > {base}.sh && {{ nohup setsid bash {base}.sh > {base}.log 2>&1 < /dev/null & }}",
                    input=script,
                    timeout=30,
                )
                if not r.ok:
                    raise JobError(f"could not start job on {node}: {r.err.strip() or r.rc}")
                rc = await self._follow(job_id, addr, 0)
            await self._finish(job_id, vmid, node, kind, snap, rc)
        except Exception as e:
            self._fail(job_id, vmid, e)

    async def _follow(self, job_id: str, addr: str, seen: int) -> int:
        """Tail the node-side log until the exit marker shows up."""
        path = f"{LOGDIR}/{job_id}.log"
        failures = 0
        while True:
            buf = b""
            gen = self.ssh.stream(addr, f"tail -n +{seen + 1} -F {path} 2>/dev/null")
            timed_out = False
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(gen.__anext__(), 600)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        timed_out = True
                        break
                    failures = 0
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        seen += 1
                        line = clean_line(raw)
                        if line.startswith(MARK):
                            return int(line.split()[1])
                        self._emit(job_id, line)
            finally:
                await gen.aclose()
            if timed_out:
                # Quiet for 10 minutes: fine if the job is still working.
                rc = await self._exit_code_if_dead(addr, job_id)
                if rc is None:
                    continue
                return rc
            failures += 1
            if failures > 20:
                raise JobError("lost the connection to the job log")
            await asyncio.sleep(min(30, 2 ** failures))

    async def _exit_code_if_dead(self, addr: str, job_id: str) -> int | None:
        """None while the job's process is alive; its exit code once it is gone."""
        # [/] keeps pgrep from matching the shell that runs it.
        alive = await self.ssh.run(addr, f"pgrep -f '[/]{LOGDIR[1:]}/{job_id}.sh' >/dev/null", timeout=30)
        if alive.ok:
            return None
        last = await self.ssh.run(addr, f"grep -a '^{MARK}' {LOGDIR}/{job_id}.log", timeout=30)
        if last.ok and last.out.split():
            return int(last.out.split()[-1])
        raise JobError("job process exited without reporting a result")

    def _emit(self, job_id: str, line: str) -> None:
        self.db.append_log(job_id, line + "\n")
        for q in self.subs.get(job_id, ()):
            q.put_nowait(line)

    def _close(self, job_id: str, vmid: int) -> None:
        self.active.pop(vmid, None)
        for q in self.subs.get(job_id, ()):
            q.put_nowait(None)

    def _fail(self, job_id: str, vmid: int, error: Exception) -> None:
        log.warning("job %s failed: %s", job_id, error)
        self._emit(job_id, f"[lxcwatch] error: {error}")
        self.db.update_job(job_id, status="failed", finished_at=time.time())
        self._close(job_id, vmid)

    async def _finish(self, job_id: str, vmid: int, node: str, kind: str, snap: str | None, rc: int) -> None:
        status = "success" if rc == 0 else "failed"
        addr = self.cluster.addr(node)
        try:
            await self.ssh.run(addr, f"rm -f {LOGDIR}/{job_id}.sh {LOGDIR}/{job_id}.log", timeout=30)
        except Exception:
            pass
        if status == "success" and snap and self.cfg.keep_snapshots > 0:
            await self._prune(job_id, vmid)
        if status == "failed" and snap and rc != 90:
            self._emit(job_id, f"[lxcwatch] update failed; snapshot {snap} is available to roll back to")
        self.db.update_job(job_id, status=status, rc=rc, finished_at=time.time())
        self._close(job_id, vmid)
        await self.cluster.scan_one(vmid)

    async def _prune(self, job_id: str, vmid: int) -> None:
        try:
            ours = [s for s in await self.cluster.snapshots(vmid) if s["name"].startswith("lxcwatch-")]
            for s in ours[self.cfg.keep_snapshots:]:
                self._emit(job_id, f"[lxcwatch] removing old snapshot {s['name']}")
                await self.cluster.delete_snapshot(vmid, s["name"])
        except Exception as e:
            self._emit(job_id, f"[lxcwatch] snapshot cleanup failed: {e}")

    # live log subscriptions

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self.subs.get(job_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self.subs[job_id]
