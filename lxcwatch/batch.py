"""'Update all': run app and/or package updates across containers, one at a time.

Each step is an ordinary job; the batch starts it, waits for it (and its
follow-up rescan) to finish, then moves on. Per container the app update runs
before the package update. The queue lives in memory: if lxcwatch restarts, the
running job is resumed as usual but the rest of the batch is dropped.
"""

import asyncio
import logging
import secrets
import time

from .db import DB
from .jobs import JobError, Jobs

log = logging.getLogger(__name__)


class BatchError(Exception):
    pass


class Batches:
    def __init__(self, db: DB, jobs: Jobs):
        self.db, self.jobs = db, jobs
        self.current: dict | None = None
        self.stop_requested = False
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return bool(self.current and self.current["status"] == "running")

    def start(self, vmids: list[int], kinds: list[str], snapshot: bool, restart: str, stop_on_failure: bool,
              only: dict[int, list[str]] | None = None) -> dict:
        """Queue `kinds` for each vmid, in order; `only` narrows the kinds per container."""
        if self.running:
            raise BatchError("an update-all run is already in progress")
        kinds = [k for k in ("app", "os") if k in kinds]
        if not kinds:
            raise BatchError("pick apps, packages or both")
        names = {c["vmid"]: c["name"] for c in self.db.containers()}
        steps = [
            {"vmid": vmid, "name": names.get(vmid, ""), "kind": kind, "status": "pending", "job": None, "note": None}
            for vmid in dict.fromkeys(vmids) if vmid in names
            for kind in kinds if not only or kind in only.get(vmid, kinds)
        ]
        if not steps:
            raise BatchError("nothing selected")
        self.current = {
            "id": secrets.token_hex(4),
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "options": {"kinds": kinds, "snapshot": snapshot, "restart": restart, "stop_on_failure": stop_on_failure},
            "steps": steps,
        }
        self.stop_requested = False
        self.task = asyncio.create_task(self._run(self.current))
        return self.current

    def stop(self) -> None:
        if self.running:
            self.stop_requested = True

    def dismiss(self) -> None:
        if not self.running:
            self.current = None

    async def _run(self, batch: dict) -> None:
        opts = batch["options"]
        try:
            for i, step in enumerate(batch["steps"]):
                if self.stop_requested:
                    for rest in batch["steps"][i:]:
                        rest["status"], rest["note"] = "skipped", "stopped"
                    batch["status"] = "stopped"
                    break
                await self._step(step, opts)
                if step["status"] == "failed" and opts["stop_on_failure"]:
                    for rest in batch["steps"][i + 1:]:
                        rest["status"], rest["note"] = "skipped", "stopped after a failure"
                    batch["status"] = "stopped"
                    break
            else:
                batch["status"] = "done"
        except Exception:
            log.exception("update-all run crashed")
            batch["status"] = "failed"
        batch["finished_at"] = time.time()

    async def _step(self, step: dict, opts: dict) -> None:
        ct = self.db.container(step["vmid"])
        info = (ct or {}).get("info") or {}
        if not ct:
            step["status"], step["note"] = "skipped", "container no longer exists"
            return
        if ct["status"] != "running":
            step["status"], step["note"] = "skipped", f"container is {ct['status']}"
            return
        if step["kind"] == "app" and not info.get("script"):
            step["status"], step["note"] = "skipped", "not a community-script"
            return
        if step["kind"] == "os" and not info.get("pkgmgr"):
            step["status"], step["note"] = "skipped", "no package manager found"
            return
        # Another job on this container (e.g. started by hand): wait for it.
        while self.jobs.busy(step["vmid"]):
            step["note"] = "waiting for another job on this container"
            await asyncio.sleep(2)
        step["note"] = None
        try:
            job_id = self.jobs.start(step["vmid"], step["kind"], opts["snapshot"], restart=opts["restart"])
        except JobError as e:
            step["status"], step["note"] = "skipped", str(e)
            return
        step["job"], step["status"] = job_id, "running"
        job = await self.jobs.wait(job_id)
        step["status"] = "success" if job and job["status"] == "success" else "failed"
        if step["status"] == "failed" and job and job["rc"] not in (None, 0):
            step["note"] = f"exit code {job['rc']}"
